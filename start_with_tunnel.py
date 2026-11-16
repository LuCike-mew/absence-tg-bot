"""
Автоматический запуск Cloudflare Tunnel + бот.

1. Запускает cloudflared tunnel --url http://localhost:PORT
2. Парсит вывод cloudflared, извлекает публичный URL (*.trycloudflare.com)
3. Сохраняет его в .env как API_URL
4. Запускает бота с API_URL в переменных окружения

Использование:
    python start_with_tunnel.py

Первичная настройка (один раз):
    cloudflared tunnel login    # откроется браузер, залогиньтесь в Cloudflare
"""

import asyncio
import subprocess
import os
import re
import sys
import time

# Добавляем корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import BASE_DIR, API_PORT

ENV_PATH = os.path.join(BASE_DIR, ".env")

# Регулярка для извлечения URL из вывода cloudflared
TUNNEL_URL_RE = re.compile(r"https?://[\w-]+\.(?:trycloudflare|cfargotunnel)\.com")


def find_cloudflared():
    """Ищет исполняемый файл cloudflared."""
    # Проверяем PATH
    if sys.platform == "win32":
        # winget устанавливает в Program Files
        candidates = [
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            r"C:\Program Files\cloudflared\cloudflared.exe",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path

    # Проверяем доступность в PATH
    try:
        subprocess.run(["cloudflared", "--version"], capture_output=True, check=True)
        return "cloudflared"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return None


def update_env_api_url(url: str):
    """Обновляет API_URL в .env файле (создаёт или заменяет)."""
    lines = []
    found = False
    if os.path.isfile(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                if line.startswith("API_URL="):
                    lines.append(f"API_URL={url}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"\n# Cloudflare Tunnel URL (автоматически обнаружен)\n")
        lines.append(f"API_URL={url}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)

    # Также устанавливаем в окружение для текущего процесса
    os.environ["API_URL"] = url


async def start_tunnel_and_bot():
    """Запускает cloudflared туннель, ждёт URL и стартует бота."""
    cloudflared = find_cloudflared()
    if not cloudflared:
        print("ОШИБКА: cloudflared не найден.")
        print("Установите его: winget install --id Cloudflare.cloudflared")
        return

    print(f"cloudflared найден: {cloudflared}")
    print(f"Запуск туннеля на localhost:{API_PORT}…")

    # Запускаем cloudflared
    proc = subprocess.Popen(
        [cloudflared, "tunnel", "--url", f"http://localhost:{API_PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tunnel_url = None
    start_time = time.time()
    timeout = 60  # секунд

    try:
        for line in proc.stdout:
            line = line.rstrip()
            print(f"[cloudflared] {line}")

            # Ищем URL в выводе
            match = TUNNEL_URL_RE.search(line)
            if match:
                tunnel_url = match.group(0)
                print(f"\n{'='*60}")
                print(f"  Туннель создан: {tunnel_url}")
                print(f"  Оживите бота — /start и сохраняйте пропуски без закрытия!")
                print(f"{'='*60}\n")

                # Сохраняем URL в .env
                update_env_api_url(tunnel_url)
                print(f"API_URL сохранён в {ENV_PATH}\n")
                break

            if time.time() - start_time > timeout:
                print("Таймаут: не удалось дождаться URL туннеля")
                proc.terminate()
                return

    except KeyboardInterrupt:
        print("\nПрервано пользователем")
        proc.terminate()
        return

    if not tunnel_url:
        print("Не удалось извлечь URL туннеля из вывода cloudflared")
        proc.terminate()
        return

    # Перезагружаем config с обновлённым API_URL
    import importlib
    import config
    importlib.reload(config)

    # Запускаем бота
    print("Запуск бота…")
    from main import main as run_bot
    try:
        await run_bot()
    except KeyboardInterrupt:
        print("\nБот остановлен")
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    print("═" * 60)
    print("  Absence Bot + Cloudflare Tunnel")
    print("═" * 60)
    print()
    print("Убедитесь, что вы выполнили ПЕРВИЧНУЮ настройку:")
    print("  cloudflared tunnel login")
    print("  (откроется браузер — войдите в Cloudflare или создайте аккаунт)")
    print()

    need_login = input("Вход выполнен? (y/n): ").strip().lower()
    if need_login not in ("y", "yes", "д", "да"):
        print("Сначала выполните: cloudflared tunnel login")
        sys.exit(0)

    asyncio.run(start_tunnel_and_bot())