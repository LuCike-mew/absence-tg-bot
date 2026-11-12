import os
import aiosqlite
from datetime import datetime, date, timedelta
from config import DB_PATH


async def init_db():
    """Создает таблицу пропусков при первом запуске."""
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
        await db.commit()


async def cleanup_old_absences():
    """Удаляет из базы пропуски старше 1-го числа предыдущего месяца."""
    today = date.today()
    first_day_this_month = today.replace(day=1)
    last_day_prev_month = first_day_this_month - timedelta(days=1)
    cutoff_date = last_day_prev_month.replace(day=1).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM absences WHERE date < ?", (cutoff_date,))
        await db.commit()


async def add_absence(user_id: int, pair_number: int, subject: str, date: str = None, reason: str = "Не указана"):
    """Записывает пропуск пользователя в БД."""
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO absences (user_id, date, pair_number, subject, reason)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, target_date, pair_number, subject, reason))
        await db.commit()


async def get_user_absences(user_id: int) -> list[tuple]:
    """Автоматически очищает устаревшие данные и возвращает актуальную историю."""
    await cleanup_old_absences()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT date, pair_number, subject, reason 
            FROM absences 
            WHERE user_id = ? 
            ORDER BY date DESC, pair_number ASC
        """, (user_id,)) as cursor:
            return await cursor.fetchall()