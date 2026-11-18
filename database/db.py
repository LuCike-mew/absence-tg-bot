import os
import time
import glob
import logging
import asyncio
import aiosqlite
from datetime import datetime, date, timedelta
from config import DB_PATH
from database.crypto import encrypt, decrypt, is_encryption_enabled

# Блокировка для сериализации операций записи в БД.
# SQLite разрешает только одного писателя в каждый момент времени, и без
# блокировки два одновременных add_absence + get_user_absences → cleanup
# могут упереться в SQLITE_BUSY. WAL-режим снимает проблему «читатели vs
# писатель», а Lock — проблему «два писателя одновременно».
_db_write_lock = asyncio.Lock()


async def init_db():
    """Создает таблицы пропусков и пользователей при первом запуске."""
    # Гарантируем, что папка базы существует (важно для собранного .exe,
    # где рядом с файлом может не быть каталога database/)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS absences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                pair_number INTEGER NOT NULL,
                subject TEXT NOT NULL,
                reason TEXT DEFAULT 'Не указана',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notify_replacements INTEGER NOT NULL DEFAULT 1
            );
        """)
        # Миграция для уже существующих баз: добавляем флаг уведомлений,
        # если таблица создавалась до появления этой колонки.
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN notify_replacements INTEGER NOT NULL DEFAULT 1"
            )
        except Exception:
            pass  # колонка уже существует
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedule_snapshots (
                week_start TEXT PRIMARY KEY,
                template_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()


async def cleanup_old_absences():
    """Удаляет из базы пропуски старше 1-го числа предыдущего месяца."""
    today = date.today()
    first_day_this_month = today.replace(day=1)
    last_day_prev_month = first_day_this_month - timedelta(days=1)
    cutoff_date = last_day_prev_month.replace(day=1)

    async with _db_write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            if is_encryption_enabled():
                # При включённом шифровании даты зашифрованы — придётся получить все,
                # расшифровать даты и удалить устаревшие по одному
                async with db.execute("SELECT id, date FROM absences") as cursor:
                    rows = await cursor.fetchall()
                ids_to_delete = []
                for row_id, enc_date in rows:
                    try:
                        dec_date = decrypt(enc_date)
                        parsed = datetime.strptime(dec_date, "%Y-%m-%d").date()
                        if parsed < cutoff_date:
                            ids_to_delete.append(row_id)
                    except (ValueError, TypeError):
                        pass
                if ids_to_delete:
                    placeholders = ",".join("?" for _ in ids_to_delete)
                    await db.execute(
                        f"DELETE FROM absences WHERE id IN ({placeholders})",
                        ids_to_delete
                    )
            else:
                await db.execute(
                    "DELETE FROM absences WHERE date < ?",
                    (cutoff_date.strftime("%Y-%m-%d"),)
                )
            await db.commit()


async def add_absence(user_id: int, pair_number: int, subject: str, date: str = None, reason: str = "Не указана") -> bool:
    """
    Записывает пропуск пользователя в БД. При включённом шифровании поля шифруются.

    Возвращает False, если такой пропуск (user_id + date + pair_number) уже существует.
    Возвращает True, если запись добавлена.
    """
    target_date = date or datetime.now().strftime("%Y-%m-%d")

    async with _db_write_lock:
        # Проверка на дубликат: получаем все пропуски, расшифровываем, сравниваем
        existing = await get_user_absences(user_id)
        for rec in existing:
            # rec: (id, date, pair_number, subject, reason)
            if rec[1] == target_date and rec[2] == pair_number:
                return False  # дубликат — не вставляем

        if is_encryption_enabled():
            enc_date = encrypt(target_date)
            enc_subject = encrypt(subject)
            enc_reason = encrypt(reason)
        else:
            enc_date = target_date
            enc_subject = subject
            enc_reason = reason

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO absences (user_id, date, pair_number, subject, reason)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, enc_date, pair_number, enc_subject, enc_reason))
            await db.commit()
    return True


async def get_user_absences(user_id: int) -> list[tuple]:
    """Возвращает историю пропусков пользователя.
    Возвращает кортежи: (id, date, pair_number, subject, reason)."""

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, date, pair_number, subject, reason 
            FROM absences 
            WHERE user_id = ? 
            ORDER BY date DESC, pair_number ASC
        """, (user_id,)) as cursor:
            rows = await cursor.fetchall()

    if not is_encryption_enabled():
        return rows

    # Расшифровываем поля
    decrypted = []
    for row in rows:
        try:
            decrypted.append((
                row[0],                 # id
                decrypt(row[1]),        # date
                row[2],                 # pair_number
                decrypt(row[3]),        # subject
                decrypt(row[4]),        # reason
            ))
        except Exception:
            decrypted.append(row)

    return decrypted


async def get_month_absence_hours(user_id: int, year: int, month: int) -> int:
    """
    Возвращает количество часов пропусков за указанный месяц.
    Каждый пропуск = 2 академических часа.
    """
    all_absences = await get_user_absences(user_id)
    count = 0
    for rec in all_absences:
        # rec: (id, date, pair_number, subject, reason)
        try:
            d = datetime.strptime(rec[1], "%Y-%m-%d").date()
            if d.year == year and d.month == month:
                count += 1
        except (ValueError, TypeError):
            pass
    return count * 2


async def delete_absence(absence_id: int, user_id: int) -> bool:
    """Удаляет пропуск по id, если он принадлежит указанному пользователю.
    Возвращает True, если запись была удалена."""
    async with _db_write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM absences WHERE id = ? AND user_id = ?",
                (absence_id, user_id)
            )
            await db.commit()
            return cursor.rowcount > 0


async def register_user(user_id: int):
    """Регистрирует пользователя в базе (если его ещё нет)."""
    async with _db_write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                (user_id,)
            )
            await db.commit()


async def get_all_user_ids() -> list[int]:
    """Возвращает список всех зарегистрированных пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def remove_user(user_id: int) -> None:
    """Удаляет пользователя из базы (например, если он заблокировал бота)."""
    async with _db_write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            await db.commit()


# ── Персональные настройки уведомлений ────────────────────────────────────────


async def get_notify_replacements(user_id: int) -> bool:
    """Возвращает, включены ли уведомления о заменах у пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT notify_replacements FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else True  # по умолчанию включено


async def set_notify_replacements(user_id: int, enabled: bool) -> None:
    """Включает/выключает уведомления о заменах для пользователя."""
    async with _db_write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET notify_replacements = ? WHERE user_id = ?",
                (1 if enabled else 0, user_id),
            )
            await db.commit()


async def get_user_ids_with_notifications() -> list[int]:
    """Возвращает id пользователей, у которых включены уведомления о заменах."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM users WHERE notify_replacements = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


# ── Ключ-значение хранилище (состояние бота между перезапусками) ──────────────
# Используется, например, для хранения отпечатка замен, чтобы уведомления о
# новых заменах переживали перезапуск процесса.


async def get_state(key: str) -> str | None:
    """Возвращает сохранённое строковое значение по ключу или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_state(key: str, value: str) -> None:
    """Сохраняет строковое значение по ключу (создаёт или перезаписывает)."""
    async with _db_write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO bot_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
            await db.commit()


# ── Снимки расписания ─────────────────────────────────────────────────────────
# Шаблон недели (weekday_index → пары), распарсенный из .xls, сохраняется
# под ключом «понедельник недели» (YYYY-MM-DD). Нужно, чтобы прошлые недели
# показывали своё расписание, а не текущее (сайт хранит старые .xls, но на
# странице видна только актуальная ссылка).


async def save_schedule_snapshot(week_start: str, template_json: str) -> None:
    """Сохраняет шаблон расписания недели (JSON-строку) под ключом недели."""
    async with _db_write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO schedule_snapshots (week_start, template_json)
                VALUES (?, ?)
                ON CONFLICT(week_start) DO UPDATE SET
                    template_json = excluded.template_json,
                    created_at = CURRENT_TIMESTAMP
                """,
                (week_start, template_json)
            )
            await db.commit()


async def get_schedule_snapshot(week_start: str) -> str | None:
    """Возвращает сохранённый шаблон недели (JSON-строку) или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT template_json FROM schedule_snapshots WHERE week_start = ?",
            (week_start,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_all_snapshot_weeks() -> list[str]:
    """Возвращает список понедельников недель, для которых есть снимки."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT week_start FROM schedule_snapshots") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


# ── Резервное копирование БД ─────────────────────────────────────────────────
# bot_data.db хранит личные пропуски — раз в сутки делаем её копию в backups/,
# чтобы порча файла не стёрла историю. Используется online-backup API SQLite
# (безопасно при работающей БД в WAL-режиме).

#: Папка для резервных копий (рядом с файлом БД).
BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), "backups")
#: Интервал между бэкапами (сутки).
BACKUP_INTERVAL_SECONDS = 24 * 3600
#: Сколько последних копий хранить (остальные удаляются при ротации).
BACKUP_KEEP_COUNT = 14
#: Ключ в bot_state с временем последнего бэкапа (epoch-секунды).
BACKUP_STATE_KEY = "last_backup"


def _rotate_backups() -> None:
    """Оставляет последние BACKUP_KEEP_COUNT копий, удаляя более старые."""
    try:
        pattern = os.path.join(BACKUP_DIR, "bot_data_*.db")
        files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        for old in files[BACKUP_KEEP_COUNT:]:
            try:
                os.remove(old)
                logging.info("Удалена старая резервная копия: %s", old)
            except OSError:
                pass
    except OSError:
        logging.exception("Ошибка ротации резервных копий")


async def backup_database() -> str:
    """Создаёт резервную копию БД через online-backup и возвращает путь."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"bot_data_{timestamp}.db")
    async with aiosqlite.connect(DB_PATH) as source:
        await source.backup(dest)
    _rotate_backups()
    logging.info("Создана резервная копия БД: %s", dest)
    return dest


async def backup_database_if_due() -> str | None:
    """
    Делает бэкап, если с прошлого прошло больше суток.

    Возвращает путь к новой копии либо None, если бэкап пока не нужен.
    """
    last_raw = await get_state(BACKUP_STATE_KEY)
    try:
        last = float(last_raw) if last_raw else 0.0
    except ValueError:
        last = 0.0

    if time.time() - last < BACKUP_INTERVAL_SECONDS:
        return None

    dest = await backup_database()
    await set_state(BACKUP_STATE_KEY, str(time.time()))
    return dest