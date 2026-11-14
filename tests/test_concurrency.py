"""
Конкурентный тест БД.

Проверяет, что aiosqlite + WAL + asyncio.Lock корректно обрабатывает
одновременные запросы от разных пользователей:
- добавление пропусков (add_absence)
- проверка дубликатов (один и тот же user+date+pair не дублируется)
- удаление пропусков (delete_absence)
- конкурентное чтение истории (get_user_absences)

Запуск: python -m tests.test_concurrency
"""

import asyncio
import logging
import sys
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Добавляем корень проекта в path
sys.path.insert(0, ".")

from database.db import (
    init_db,
    add_absence,
    get_user_absences,
    delete_absence,
    get_month_absence_hours,
    cleanup_old_absences,
)


async def test_parallel_add_different_users():
    """10 пользователей одновременно отправляют пропуски."""
    logger.info("=== Тест 1: параллельное добавление разными пользователями ===")
    today = date.today().isoformat()

    async def add_for_user(uid: int):
        results = []
        for pair in range(1, 5):
            ok = await add_absence(uid, pair, f"Предмет-{pair}", today)
            results.append(ok)
        return uid, results

    tasks = [add_for_user(i) for i in range(1000, 1010)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        logger.error("ОШИБКИ при параллельном добавлении:")
        for e in errors:
            logger.error(f"  {e}")
        return False

    for uid, res in results:
        assert all(res), f"Пользователь {uid}: не все вставки успешны: {res}"
        logger.info(f"  user={uid}: {len(res)} пропусков сохранено ✓")

    logger.info("Тест 1 пройден ✓")
    return True


async def test_parallel_duplicate_prevention():
    """Один пользователь пытается сохранить одно и то же 10 раз параллельно."""
    logger.info("=== Тест 2: защита от дубликатов при параллельных запросах ===")
    today = date.today().isoformat()

    async def try_add():
        return await add_absence(9999, 1, "Дубликат-тест", today)

    tasks = [try_add() for _ in range(10)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        logger.error("ОШИБКИ при проверке дубликатов:")
        for e in errors:
            logger.error(f"  {e}")
        return False

    success_count = sum(1 for r in results if r is True)
    fail_count = sum(1 for r in results if r is False)

    logger.info(f"  Успешных вставок: {success_count}, отклонено дубликатов: {fail_count}")
    assert success_count == 1, f"Должна быть ровно 1 вставка, а не {success_count}"
    logger.info("Тест 2 пройден ✓")
    return True


async def test_parallel_read_write():
    """Одновременное чтение истории и добавление новых пропусков."""
    logger.info("=== Тест 3: параллельное чтение + запись ===")
    today = date.today().isoformat()

    async def writer(uid: int):
        for pair in range(1, 4):
            await add_absence(uid, pair, f"RW-предмет-{pair}", today)
        return uid

    async def reader(uid: int):
        return await get_user_absences(uid)

    tasks = []
    # 5 писателей
    for i in range(2000, 2005):
        tasks.append(writer(i))
    # 5 читателей (читают «чужих» пользователей, чтобы не зависеть от порядка)
    for i in range(1000, 1010):
        tasks.append(reader(i))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]

    if errors:
        logger.error("ОШИБКИ при параллельном чтении/записи:")
        for e in errors:
            logger.error(f"  {e}")
        return False

    logger.info(f"  Всего операций: {len(results)}, ошибок: {len(errors)} ✓")
    logger.info("Тест 3 пройден ✓")
    return True


async def test_parallel_delete():
    """Параллельное добавление и удаление."""
    logger.info("=== Тест 4: параллельное добавление + удаление ===")
    today = date.today().isoformat()

    # Сначала добавляем пропуски для пользователя 3000
    for pair in range(1, 6):
        await add_absence(3000, pair, f"Del-предмет-{pair}", today)

    # Получаем id добавленных записей
    all_recs = await get_user_absences(3000)
    ids = [r[0] for r in all_recs if r[2] <= 5]
    logger.info(f"  Добавлено записей: {len(ids)}")

    # Параллельно удаляем
    async def deleter(absence_id):
        return await delete_absence(absence_id, 3000)

    tasks = [deleter(aid) for aid in ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        logger.error("ОШИБКИ при параллельном удалении:")
        for e in errors:
            logger.error(f"  {e}")
        return False

    deleted = sum(1 for r in results if r is True)
    logger.info(f"  Удалено: {deleted} из {len(ids)} ✓")
    logger.info("Тест 4 пройден ✓")
    return True


async def main():
    logger.info("Инициализация БД…")
    await init_db()

    tests = [
        test_parallel_add_different_users,
        test_parallel_duplicate_prevention,
        test_parallel_read_write,
        test_parallel_delete,
    ]

    passed = 0
    for test in tests:
        try:
            if await test():
                passed += 1
        except Exception as e:
            logger.exception(f"Тест упал: {e}")

    logger.info(f"\n{'='*40}")
    logger.info(f"Результат: {passed}/{len(tests)} тестов пройдено")

    if passed == len(tests):
        logger.info("✅ Все тесты пройдены — конкурентность работает корректно")
    else:
        logger.error("❌ Некоторые тесты НЕ пройдены")

    # Убираем тестовые данные
    logger.info("Очистка тестовых данных…")
    await cleanup_old_absences()


if __name__ == "__main__":
    asyncio.run(main())