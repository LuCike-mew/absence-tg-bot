"""Шифрование данных в БД через Fernet (AES-128)."""

import os
from cryptography.fernet import Fernet

# Ключ читается из переменной окружения ENCRYPTION_KEY (файл .env).
# Если ключ не задан — шифрование отключено, данные хранятся открыто.
_ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

_fernet: Fernet | None = None

if _ENCRYPTION_KEY:
    try:
        _fernet = Fernet(_ENCRYPTION_KEY.encode("utf-8"))
    except Exception:
        _fernet = None


def is_encryption_enabled() -> bool:
    """Проверяет, включено ли шифрование (задан ли ENCRYPTION_KEY)."""
    return _fernet is not None


def generate_key() -> str:
    """Генерирует новый Fernet-ключ. Используется при первой настройке."""
    return Fernet.generate_key().decode("utf-8")


def encrypt(text: str) -> str:
    """Шифрует строку. Если шифрование отключено — возвращает как есть."""
    if _fernet is None or not text:
        return text
    return _fernet.encrypt(text.encode("utf-8")).decode("utf-8")


def decrypt(text: str) -> str:
    """Расшифровывает строку. Если шифрование отключено — возвращает как есть."""
    if _fernet is None or not text:
        return text
    try:
        return _fernet.decrypt(text.encode("utf-8")).decode("utf-8")
    except Exception:
        # Если не удалось расшифровать (например, старые незашифрованные данные) —
        # возвращаем как есть
        return text