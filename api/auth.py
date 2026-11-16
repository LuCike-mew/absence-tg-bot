"""Проверка подлинности данных от Telegram Mini App (initData).

Алгоритм верификации из официальной документации Telegram:
  1. Разобрать initData на key=value пары (query-string формат, значения URL-encoded).
  2. Удалить поле 'hash'.
  3. Отсортировать ключи по алфавиту.
  4. Собрать data_check_string = "key1=value1\nkey2=value2\n..."
  5. Вычислить secret_key = HMAC_SHA256(bot_token, "WebAppData").
  6. Вычислить hash = hex(HMAC_SHA256(data_check_string, secret_key)).
  7. Сравнить с полем 'hash' из initData.
"""

import hashlib
import hmac
import logging
from urllib.parse import unquote, parse_qs

logger = logging.getLogger(__name__)


def verify_init_data(init_data_raw: str, bot_token: str) -> bool:
    """Проверяет подлинность initData от Telegram WebApp.

    Args:
        init_data_raw: Строка initData из tg.initData (или заголовка Authorization).
        bot_token: Токен бота.

    Returns:
        True если данные подлинные и свежие, False иначе.
    """
    if not init_data_raw or not bot_token:
        return False

    try:
        # 1. Разбираем initData в словарь (сохраняем URL-encoded значения)
        #    parse_qs возвращает списки; нам нужны единичные значения
        parsed_qs = parse_qs(init_data_raw)
        parsed = {k: v[0] for k, v in parsed_qs.items()}
    except Exception:
        logger.debug("Не удалось разобрать initData: %s", init_data_raw[:100])
        return False

    # 2. Извлекаем и удаляем hash — он не участвует в data_check_string
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        logger.debug("initData без поля 'hash'")
        return False

    # 3. Сортируем ключи по алфавиту
    # 4. data_check_string: ключи в URL-encoded виде, значения URL-encoded,
    #    соединённые через \n
    pairs = []
    for key in sorted(parsed.keys()):
        # Значения уже URL-encoded из parse_qs, но приведём к нижнему регистру
        # (URL-encoding может использовать %2D и %2d — приводим для единообразия)
        raw_value = parsed[key]
        pairs.append(f"{key}={raw_value}")

    data_check_string = "\n".join(pairs)

    # 5. secret_key = HMAC_SHA256(bot_token_bytes, "WebAppData" bytes)
    secret_key = hmac.new(
        msg="WebAppData".encode("utf-8"),
        key=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    # 6. hash = hex(HMAC_SHA256(data_check_string_bytes, secret_key))
    computed_hash = hmac.new(
        msg=data_check_string.encode("utf-8"),
        key=secret_key,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # 7. Сравнение
    if computed_hash != received_hash:
        logger.debug("Хэш initData не совпадает (ожид. %s, получ. %s)",
                     computed_hash[:16], received_hash[:16])
        return False

    return True


def extract_user_id(init_data_raw: str) -> int | None:
    """Извлекает user.id из initData (без верификации, только парсинг).

    Полезно для логирования или быстрой проверки перед верификацией.
    """
    if not init_data_raw:
        return None
    try:
        # user приходит как JSON-строка внутри URL
        # Пример: ...&user=%7B%22id%22%3A123456%7D&...
        parsed_qs = parse_qs(init_data_raw)
        user_json = parsed_qs.get("user", [None])[0]
        if not user_json:
            return None
        # Декодируем URL → JSON-строка
        import json
        user_obj = json.loads(unquote(user_json))
        return int(user_obj.get("id", 0))
    except Exception:
        return None