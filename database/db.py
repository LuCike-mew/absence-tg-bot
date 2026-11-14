import os
import aiosqlite
from datetime import datetime, date, timedelta
from config import DB_PATH
from database.crypto import encrypt, decrypt, is_encryption_enabled


async def init_db():
    """Создает таблицы пропусков и пользователей при первом запуске."""
    # Гарантируем, что папка базы существует (важно для собранного .exe,
    # где рядом с файлом может не быть каталога database/)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
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
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()


async def cleanup_old_absences():
    """Удаляет из базы пропуски старше 1-го числа предыдущего месяца."""
    today = date.today()
    first_day_this_month = today.replace(day=1)
    last_day_prev_month = first_day_this_month - timedelta(days=1)
    cutoff_date = last_day_prev_month.replace(day=1)

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
    """Автоматически очищает устаревшие данные и возвращает актуальную историю.
    Возвращает кортежи: (id, date, pair_number, subject, reason)."""
    await cleanup_old_absences()

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
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM absences WHERE id = ? AND user_id = ?",
            (absence_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def register_user(user_id: int):
    """Регистрирует пользователя в базе (если его ещё нет)."""
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