import asyncio
import sys
import logging
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from config import BOT_TOKEN
from database.db import init_db
from bot.handlers import router

# Принудительно переводим stdout/stderr в UTF-8. Иначе при запуске из exe
# (пайп с GUI-панелью) кириллица в логах кодируется в cp1251, а читающая
# сторона ждёт UTF-8 — ломается чтение логов и бот виснет.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

logging.basicConfig(level=logging.INFO)

# Описание бота: отображается в профиле бота (клик по нику) и при старте чата.
BOT_DESCRIPTION = (
    "📅 Бот группы ВЕБ-42 — актуальное расписание и замены с сайта колледжа.\n\n"
    "Возможности:\n"
    "• расписание на неделю с аудиториями и заменами;\n"
    "• отметка пропусков с сохранением личной истории;\n"
    "• подтверждение после каждого сохранения.\n\n"
    "Как открыть: нажмите кнопку «Отметить пропуски» под чатом."
)

BOT_SHORT_DESCRIPTION = "Расписание, замены и отметка пропусков для группы ВЕБ-42."


async def set_main_menu(bot: Bot):
    """Регистрация команд в кнопке 'Меню' Telegram."""
    main_menu_commands = [
        BotCommand(command="/start", description="Запустить бота и открыть приложение"),
        BotCommand(command="/about", description="О проекте и авторах"),
    ]
    await bot.set_my_commands(main_menu_commands)


async def set_bot_profile(bot: Bot):
    """Устанавливает описание и короткое описание бота (профиль в Telegram)."""
    await bot.set_my_description(BOT_DESCRIPTION)
    await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)


async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Настройка кнопки Меню и профиля бота перед стартом опроса
    await set_main_menu(bot)
    await set_bot_profile(bot)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())