import json
import gzip
import base64
import logging
import urllib.parse
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, MenuButtonWebApp, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton
from parser.schedule import get_full_week_schedule
from database.db import add_absence, get_user_absences
from config import WEB_APP_URL

router = Router()


def _gzip_b64(text: str) -> str:
    """Сжимает строку через gzip и кодирует в url-safe base64 без '='."""
    compressed = gzip.compress(text.encode("utf-8"))
    return base64.urlsafe_b64encode(compressed).decode("utf-8").rstrip("=")


def compress_history(history_records: list[tuple]) -> str:
    """Сжимает историю пропусков в компактную строку Base64."""
    if not history_records:
        return ""

    # Формат строк: YYYY-MM-DD|номер_пары|Предмет
    raw_lines = [f"{rec[0]}|{rec[1]}|{rec[2]}" for rec in history_records]
    text_data = "\n".join(raw_lines)
    return _gzip_b64(text_data)


async def get_dynamic_webapp_url(user_id: int) -> str:
    """Формирует URL со сжатым расписанием и историей."""
    week_schedule = await get_full_week_schedule()
    history_records = await get_user_absences(user_id)

    # Сжимаем оба параметра, чтобы влезть в лимит Telegram WebApp URL
    compressed_week = _gzip_b64(json.dumps(week_schedule, ensure_ascii=False))
    compressed_history = compress_history(history_records)

    encoded_week = urllib.parse.quote(compressed_week)
    encoded_history = urllib.parse.quote(compressed_history) if compressed_history else ""

    return f"{WEB_APP_URL}?v=11&week={encoded_week}&h={encoded_history}"


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Настраивает кнопку вызова Web App в меню и клавиатуре."""
    user_id = message.from_user.id
    target_url = await get_dynamic_webapp_url(user_id)

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Отметить пропуски", web_app=WebAppInfo(url=target_url))]
        ],
        resize_keyboard=True
    )

    await message.bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(
            text="Открыть приложение",
            web_app=WebAppInfo(url=target_url)
        )
    )

    await message.answer(
        "👋 Привет! Это бот для отметки пропусков группы <b>ВЕБ-42</b>.\n\n"
        "Что здесь можно:\n"
        "📅 — открыть актуальное расписание с заменами и аудиториями\n"
        "✍️ — отметить пропуски и сохранить их в личную историю\n\n"
        "Как отметить пропуск:\n"
        "1️⃣ Нажмите кнопку <b>«Отметить пропуски»</b> под чатом.\n"
        "2️⃣ Выберите пары, на которых вас не будет.\n"
        "3️⃣ Нажмите «Сохранить пропуски» внутри приложения.\n\n"
        "Готово — пропуск появится в вашей истории. Начните с кнопки ниже 👇",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@router.message(Command("about"))
async def cmd_about(message: Message):
    """Выводит информацию о проекте и авторах."""
    about_text = (
        "<b>MiraiBot — Расписание и учет пропусков</b>\n\n"
        "Проект создан для удобного отслеживания актуального учебного расписания, "
        "замен и ведения личного учета посещаемости.\n\n"
        "<b>Основные возможности:</b>\n"
        "— Автоматическое получение расписания и замен с сайта колледжа\n"
        "— Удобная система отметки и просмотра пропусков через Web App\n"
        "— Полная конфиденциальность и персональное хранение данных\n\n"
        "<b>Как пользоваться:</b>\n"
        "— Чтобы отметить пропуск, нажмите кнопку «Отметить пропуски» под чатом\n"
        "— Чтобы посмотреть расписание и историю пропусков, нажмите синюю кнопку слева от поля ввода\n\n"
        "<b>Разработка и реализация:</b> @LuCike_mew\n"
        "<b>Вдохновитель и соавтор:</b> @a_korop (за оригинальную концепцию парсинга расписания)\n\n"
        "<b>Исходный код:</b> <a href=\"https://github.com/LuCike-mew/absence-tg-bot\">github.com/LuCike-mew/absence-tg-bot</a>\n\n"
        "Проект распространяется бесплатно для студентов группы ВЕБ-42."
    )
    await message.answer(about_text, parse_mode="HTML")


@router.message(F.web_app_data)
async def process_web_app_data(message: Message):
    """Принимает пропуски от Web App, пишет их в БД и подтверждает пользователю."""
    raw_data = message.web_app_data.data

    try:
        data = json.loads(raw_data)
    except (ValueError, TypeError):
        logging.warning("Web App прислал некорректный JSON: %r", raw_data)
        await message.answer("⚠️ Не удалось разобрать данные, попробуйте ещё раз.")
        return

    records = data.get("absences", [])
    if not records:
        return

    saved = 0
    subjects = []
    for item in records:
        await add_absence(
            user_id=message.from_user.id,
            pair_number=item["pair_number"],
            subject=item["subject"],
            date=item.get("date")
        )
        saved += 1
        subjects.append(f"{item['pair_number']} пара — {item['subject']}")

    # Подтверждение пользователю
    lines = "\n".join(f"• {s}" for s in subjects)
    await message.answer(
        f"✅ Сохранено пропусков: {saved}\n{lines}",
        parse_mode="HTML"
    )

    # Обновление URL в главной кнопке меню для следующего открытия
    new_url = await get_dynamic_webapp_url(message.from_user.id)
    await message.bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(
            text="Открыть приложение",
            web_app=WebAppInfo(url=new_url)
        )
    )