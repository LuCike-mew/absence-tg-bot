import json
import gzip
import base64
import logging
import urllib.parse
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, MenuButtonWebApp, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton
from parser.schedule import get_month_schedule
from database.db import add_absence, get_user_absences, register_user, get_all_user_ids, delete_absence, get_month_absence_hours
from config import WEB_APP_URL, ADMIN_USER_ID

router = Router()


def _gzip_b64(text: str) -> str:
    """Сжимает строку через gzip и кодирует в url-safe base64 без '='."""
    compressed = gzip.compress(text.encode("utf-8"))
    return base64.urlsafe_b64encode(compressed).decode("utf-8").rstrip("=")


def compress_history(history_records: list[tuple]) -> str:
    """Сжимает историю пропусков в компактную строку Base64."""
    if not history_records:
        return ""

    # Формат строк: ID|YYYY-MM-DD|номер_пары|Предмет
    # rec: (id, date, pair_number, subject, reason)
    raw_lines = [f"{rec[0]}|{rec[1]}|{rec[2]}|{rec[3]}" for rec in history_records]
    text_data = "\n".join(raw_lines)
    return _gzip_b64(text_data)


async def get_dynamic_webapp_url(user_id: int) -> str:
    """Формирует URL со сжатым расписанием и историей."""
    from datetime import date
    month_schedule = await get_month_schedule()
    history_records = await get_user_absences(user_id)

    # Первое число месяца — чтобы WebApp знал границу навигации
    today = date.today()
    month_start = date(today.year, today.month, 1).isoformat()

    # Сжимаем оба параметра, чтобы влезть в лимит Telegram WebApp URL
    compressed_week = _gzip_b64(json.dumps(month_schedule, ensure_ascii=False))
    compressed_history = compress_history(history_records)

    encoded_week = urllib.parse.quote(compressed_week)
    encoded_history = urllib.parse.quote(compressed_history) if compressed_history else ""
    encoded_month_start = urllib.parse.quote(month_start)

    return f"{WEB_APP_URL}?v=13&week={encoded_week}&h={encoded_history}&ms={encoded_month_start}"


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Настраивает кнопку вызова Web App в меню и клавиатуре."""
    user_id = message.from_user.id
    target_url = await get_dynamic_webapp_url(user_id)

    # Регистрируем пользователя для будущих рассылок
    await register_user(user_id)

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


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Показывает количество зарегистрированных пользователей (только для админа)."""
    if message.from_user.id != ADMIN_USER_ID:
        await message.answer("⛔ Эта команда доступна только администратору.")
        return

    all_users = await get_all_user_ids()
    total = len(all_users)

    await message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>",
        parse_mode="HTML"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """Рассылает сообщение всем пользователям бота (только для админа)."""
    user_id = message.from_user.id

    if user_id != ADMIN_USER_ID:
        await message.answer("⛔ Эта команда доступна только администратору.")
        return

    # Извлекаем текст после команды
    text = message.text or ""
    # Удаляем команду /broadcast и возможное упоминание бота
    if text.startswith("/broadcast"):
        text = text[len("/broadcast"):].strip()
        # Если команда написана как /broadcast@bot_username — обрабатываем и этот случай
        if text.startswith("@"):
            space_idx = text.find(" ")
            if space_idx != -1:
                text = text[space_idx:].strip()
            else:
                text = ""

    if not text:
        await message.answer(
            "📝 <b>Использование:</b>\n"
            "<code>/broadcast ваше сообщение</code>\n\n"
            "Сообщение будет отправлено всем пользователям бота.",
            parse_mode="HTML"
        )
        return

    # Получаем всех пользователей
    all_users = await get_all_user_ids()
    total = len(all_users)

    if total == 0:
        await message.answer("📭 Нет зарегистрированных пользователей для рассылки.")
        return

    # Отправляем сообщение каждому пользователю
    sent = 0
    failed = 0
    for uid in all_users:
        try:
            await message.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1

    # Отчёт админу
    await message.answer(
        f"📊 <b>Рассылка завершена</b>\n\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Не удалось: {failed}\n"
        f"👥 Всего пользователей: {total}",
        parse_mode="HTML"
    )


@router.message(F.web_app_data)
async def process_web_app_data(message: Message):
    """Принимает данные от Web App: сохранение пропусков или их удаление."""
    raw_data = message.web_app_data.data

    try:
        data = json.loads(raw_data)
    except (ValueError, TypeError):
        logging.warning("Web App прислал некорректный JSON: %r", raw_data)
        await message.answer("⚠️ Не удалось разобрать данные, попробуйте ещё раз.")
        return

    user_id = message.from_user.id
    response_text = None

    # ── Обработка удалений ──────────────────────────────────────────────
    deletions = data.get("deletions", [])
    if deletions:
        deleted_count = 0
        for item in deletions:
            success = await delete_absence(
                absence_id=item["id"],
                user_id=user_id
            )
            if success:
                deleted_count += 1

        response_text = f"🗑 Удалено пропусков: {deleted_count}"

    # ── Обработка сохранения пропусков ──────────────────────────────────
    records = data.get("absences", [])
    if records:
        saved = 0
        skipped = 0
        subjects = []
        for item in records:
            inserted = await add_absence(
                user_id=user_id,
                pair_number=item["pair_number"],
                subject=item["subject"],
                date=item.get("date")
            )
            if inserted:
                saved += 1
                # Форматируем дату: YYYY-MM-DD → DD.MM
                date_str = item.get("date", "")
                if len(date_str) == 10 and date_str[4] == "-":
                    date_str = f"{date_str[8:10]}.{date_str[5:7]}"
                subjects.append(f"{date_str} {item['pair_number']} пара — {item['subject']}")
            else:
                skipped += 1

        lines = "\n".join(f"• {s}" for s in subjects)
        response_text = f"✅ Сохранено пропусков: {saved}"
        if skipped > 0:
            response_text += f"\n⚠️ Пропущено дубликатов: {skipped}"
        response_text += f"\n{lines}"

        # Проверка на превышение 30 часов за текущий месяц
        from datetime import date as dt_date
        today = dt_date.today()
        month_hours = await get_month_absence_hours(user_id, today.year, today.month)
        if month_hours >= 30:
            response_text += (
                f"\n\n⚠️ <b>Внимание!</b> У вас уже <b>{month_hours} часов</b> пропусков "
                f"за этот месяц. Это больше 30 часов — возможен выговор. "
                f"Не забудьте написать объяснительные и постарайтесь чаще посещать занятия."
            )

    if response_text is None:
        return

    # Формируем свежий URL и обновляем обе кнопки одним сообщением
    new_url = await get_dynamic_webapp_url(user_id)
    await message.bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(
            text="Открыть приложение",
            web_app=WebAppInfo(url=new_url)
        )
    )
    await message.answer(
        response_text,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Отметить пропуски", web_app=WebAppInfo(url=new_url))]
            ],
            resize_keyboard=True
        )
    )