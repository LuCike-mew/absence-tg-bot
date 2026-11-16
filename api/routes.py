"""FastAPI сервер — REST API для фронтенда Telegram WebApp.

Эндпоинты:
  GET  /api/health                — проверка живости сервера
  GET  /api/schedule              — актуальное расписание (без авторизации)
  GET  /api/history?user_id=...   — история пропусков (авторизация по initData)
  POST /api/absences              — сохранить пропуски
  DELETE /api/absences/{id}       — удалить пропуск
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import logging

from api.auth import verify_init_data, extract_user_id
from parser.schedule import get_cached_month_schedule
from database.db import add_absence, get_user_absences, delete_absence, get_month_absence_hours

logger = logging.getLogger(__name__)

app = FastAPI(title="Absence Bot API", version="1.0.0")

# CORS: разрешаем запросы из любого источника (фронтенд на GitHub Pages)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────── Вспомогательные функции ──────────────────────────────

def _get_bot_token() -> str:
    """Ленивая загрузка BOT_TOKEN (после загрузки .env)."""
    from config import BOT_TOKEN
    return BOT_TOKEN


async def _authenticate(request: Request) -> int:
    """Проверяет initData в заголовке Authorization и возвращает user_id.

    Raises:
        HTTPException(401) если данные невалидны или отсутствуют.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("twa "):
        raise HTTPException(status_code=401, detail="Отсутствует токен авторизации")

    init_data = auth_header[4:]  # обрезаем "twa "
    bot_token = _get_bot_token()

    if not verify_init_data(init_data, bot_token):
        raise HTTPException(status_code=401, detail="Недействительная подпись данных")

    user_id = extract_user_id(init_data)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Не удалось извлечь user_id")

    return user_id


# ─────────────────── Эндпоинты ────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """Проверка живости API (используется фронтендом и туннелем)."""
    return {"status": "ok"}


@app.get("/api/schedule")
async def get_schedule():
    """Возвращает закэшированное расписание на текущий месяц.

    Не требует авторизации — данные публичные.
    """
    schedule = await get_cached_month_schedule()
    return {"schedule": schedule}


@app.get("/api/history")
async def get_history(request: Request):
    """Возвращает историю пропусков текущего пользователя.

    Требуется заголовок Authorization: twa <initData>
    """
    user_id = await _authenticate(request)
    records = await get_user_absences(user_id)

    history = [
        {
            "id": rec[0],
            "date": rec[1],
            "pair_num": rec[2],
            "subject": rec[3],
        }
        for rec in records
    ]
    return {"history": history}


@app.post("/api/absences")
async def save_absences(request: Request):
    """Сохраняет пропуски (один или несколько).

    Тело запроса: { "absences": [{ "pair_number": int, "subject": str, "date": str }, ...] }
    """
    user_id = await _authenticate(request)
    body = await request.json()
    absences = body.get("absences", [])

    if not absences:
        raise HTTPException(status_code=400, detail="Список пропусков пуст")

    saved = 0
    skipped = 0
    details = []

    for item in absences:
        inserted = await add_absence(
            user_id=user_id,
            pair_number=item["pair_number"],
            subject=item["subject"],
            date=item.get("date"),
        )
        if inserted:
            saved += 1
            details.append({
                "pair_number": item["pair_number"],
                "subject": item["subject"],
                "date": item.get("date", ""),
            })
        else:
            skipped += 1

    # Проверка на превышение 30 часов
    from datetime import date as dt_date
    today = dt_date.today()
    month_hours = await get_month_absence_hours(user_id, today.year, today.month)
    warning = None
    if month_hours >= 30:
        warning = (
            f"⚠️ У вас уже {month_hours} часов пропусков за этот месяц. "
            f"Это больше 30 часов — возможен выговор."
        )

    return {
        "saved": saved,
        "skipped": skipped,
        "details": details,
        "month_hours": month_hours,
        "warning": warning,
    }


@app.delete("/api/absences/{absence_id}")
async def remove_absence(absence_id: int, request: Request):
    """Удаляет пропуск по ID.

    Пользователь может удалять только свои пропуски.
    """
    user_id = await _authenticate(request)
    success = await delete_absence(absence_id=absence_id, user_id=user_id)

    if not success:
        raise HTTPException(status_code=404, detail="Пропуск не найден или уже удалён")

    return {"deleted": True, "id": absence_id}