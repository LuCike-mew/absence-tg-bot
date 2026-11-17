import asyncio
import sys
import logging
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from config import BOT_TOKEN
from database.db import init_db, cleanup_old_absences
from bot.handlers import router
from parser.schedule import refresh_schedule_cache

# ── Самовосстановление ────────────────────────────────────────────────────────
# Если сеть пропала на миг (мигание интернета, перезагрузка роутера, обрыв у
# хостера), цикл опроса Telegram падает и процесс завершается. Ниже —
# «супервайзер»: он ловит любые сбои и повторяет попытки раз в минуту, пока
# соединение не восстановится. Бот поднимается сам, без вмешательства из GUI.
RETRY_DELAY_SECONDS = 60  # интервал между попытками восстановления
_startup_done = False    # первичная настройка (БД, меню, кэш) выполнена хотя бы раз

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
        BotCommand(command="/broadcast", description="Рассылка сообщений (только админ)"),
        BotCommand(command="/stats", description="Статистика бота (только админ)"),
    ]
    await bot.set_my_commands(main_menu_commands)


async def set_bot_profile(bot: Bot):
    """Устанавливает описание и короткое описание бота (профиль в Telegram)."""
    await bot.set_my_description(BOT_DESCRIPTION)
    await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)


async def schedule_refresh_loop(bot: Bot):
    """Фоновый цикл: раз в час обновляет кэш, чистит пропуски и обновляет кнопки WebApp."""
    from database.db import get_all_user_ids
    from bot.handlers import get_dynamic_webapp_url
    from aiogram.types import MenuButtonWebApp, WebAppInfo

    while True:
        await asyncio.sleep(3600)
        try:
            await cleanup_old_absences()
        except Exception:
            logging.exception("Ошибка очистки старых пропусков")
        try:
            await refresh_schedule_cache()
        except Exception:
            logging.exception("Ошибка фонового обновления расписания")
        # После обновления кэша — обновляем WebApp-кнопку у всех пользователей
        try:
            user_ids = await get_all_user_ids()
            logging.info("Обновление WebApp-кнопок для %d пользователей…", len(user_ids))
            for user_id in user_ids:
                try:
                    target_url = await get_dynamic_webapp_url(user_id)
                    await bot.set_chat_menu_button(
                        chat_id=user_id,
                        menu_button=MenuButtonWebApp(
                            text="Открыть приложение",
                            web_app=WebAppInfo(url=target_url),
                        ),
                    )
                    await asyncio.sleep(0.05)  # ≈20 запросов/сек во избежание лимитов
                except Exception:
                    continue
            logging.info("Кнопки WebApp обновлены")
        except Exception:
            logging.exception("Ошибка обновления кнопок WebApp")


async def _safe_run(coro, description: str):
    """Выполняет корутину, гася ошибку: сбой сети не должен рушить запуск."""
    try:
        await coro
    except Exception:
        logging.exception("Не удалось выполнить '%s' (продолжаю работу)", description)


async def _initial_setup(bot: Bot):
    """Разовая настройка перед первым опросом Telegram.

    Каждый шаг защищён: если нет сети или упал запрос — просто логируем и идём
    дальше, чтобы бот всё равно стартовал и подключился при появлении связи.
    """
    await _safe_run(init_db(), "инициализация БД")

    logging.info("Первичная загрузка расписания…")
    await _safe_run(refresh_schedule_cache(), "первичная загрузка расписания")

    await _safe_run(set_main_menu(bot), "регистрация команд меню")
    await _safe_run(set_bot_profile(bot), "установка профиля бота")


async def run_bot(bot: Bot, dp: Dispatcher):
    """Супервайзер: держит бота в работе, восстанавливаясь после любой ошибки.

    Логика:
    - первый запуск выполняет разовую настройку (БД, расписание, меню);
    - цикл опроса Telegram перезапускается при сетевом сбое;
    - интервал между попытками — RETRY_DELAY_SECONDS (раз в минуту), пока
      соединение не восстановится;
    - фоновое обновление расписания поднимается один раз и живёт всё время.
    """
    global _startup_done

    if not _startup_done:
        await _initial_setup(bot)
        # Фоновый цикл обновления запускаем один раз — в отдельной «песочнице»
        # ошибок, чтобы его падение не привлекало внимание супервайзера.
        asyncio.create_task(
            _safe_run(schedule_refresh_loop(bot), "фоновое обновление")
        )
        _startup_done = True

    while True:
        try:
            # drop_pending_updates=False — не теряем накопившиеся сообщения
            await dp.start_polling(bot)
            return  # штатное завершение (например, при остановке)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning(
                "Цикл опроса Telegram прерван (%s: %s). "
                "Перезапуск через %d сек…",
                type(exc).__name__, exc, RETRY_DELAY_SECONDS,
            )
        # Ждём перед следующей попыткой; отмену пробрасываем наружу
        try:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
        except asyncio.CancelledError:
            raise


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await run_bot(bot, dp)


if __name__ == "__main__":
    asyncio.run(main())