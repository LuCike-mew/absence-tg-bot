import os
import sys


def _base_dir() -> str:
    """Корневая папка: рядом с .exe (собранный вид) или с config.py (dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _base_dir()


def _load_dotenv() -> None:
    """Загружает переменные окружения из файла .env (секреты не хранятся в коде)."""
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except OSError:
        pass


_load_dotenv()

# Токен бота берётся из переменной окружения BOT_TOKEN (файл .env рядом с
# исполняемым файлом). Пример заполнения см. в .env.example.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

GROUP_NAME = "ВЕБ-42" #Впишите название своей группы как в .xls файле расписания
COLLEGE_URL = "https://rgkript.ru/"

# Резервная ссылка на случай, если на главной странице нет файлов расписания
DIRECT_SCHEDULE_URL = "https://rgkript.ru/wp-content/uploads//2026/06/29.06.2026-dlya-sajta_crd.xls"

DB_PATH = os.path.join(BASE_DIR, "database", "bot_data.db")

WEB_APP_URL = os.getenv("WEB_APP_URL", "https://lucike-mew.github.io/absence-webapp/")

# Флаг для переключения между тестовыми моками и реальным парсингом
# True  — использовать тестовые моки (не ходит в сеть)
# False — реальный парсинг с сайта колледжа
USE_MOCK_SCHEDULE = os.getenv("USE_MOCK_SCHEDULE", "false").lower() in ("1", "true", "yes")
