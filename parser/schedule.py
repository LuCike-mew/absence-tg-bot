"""
Парсер расписания с сайта колледжа.

Поддерживает два режима:
- Реальный: загружает .xls с сайта и парсит его через xlrd.
- Mock:    возвращает тестовые данные без выхода в сеть.

Переключение — флаг USE_MOCK_SCHEDULE в config.py.
"""

import io
import logging
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
import xlrd

from config import (
    COLLEGE_URL,
    GROUP_NAME,
    DIRECT_SCHEDULE_URL,
    USE_MOCK_SCHEDULE,
)

SCHEDULE_PAGE_URL = urljoin(COLLEGE_URL, "raspisanie-zanyatiy/")
REPLACEMENTS_PAGE_URL = SCHEDULE_PAGE_URL
DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
TIME_RE = re.compile(r"^\s*\d{1,2}[.:]\d{2}\s*[-–]\s*\d{1,2}[.:]\d{2}\s*$")

# Браузерный User-Agent, чтобы сервер не блокировал запросы
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# ── mock helpers ──────────────────────────────────────────────────────────────

_MOCK_SCHEDULE_CACHE: list | None = None  # кэш на время жизни процесса


def _build_mock_week() -> list:
    """Генерирует тестовое расписание на текущую неделю (6 дней)."""
    start = datetime.now().date() - timedelta(days=datetime.now().weekday())
    result = []
    for i, day_name in enumerate(DAYS):
        current = start + timedelta(days=i)
        mock_pairs = [
            {"num": 1, "subject": "МДК 09.01", "room": "112", "is_replacement": False},
            {"num": 2, "subject": "Сосновский А.И.", "room": "ВЦ-4", "is_replacement": True},
            {"num": 3, "subject": "Физическая культура", "room": "с/з", "is_replacement": False},
        ] if i < 5 else []  # суббота — выходной
        result.append({
            "day_name": day_name,
            "date": current.strftime("%d.%m"),
            "full_date": current.isoformat(),
            "pairs": mock_pairs,
        })
    return result


# ── HTML parsing ──────────────────────────────────────────────────────────────

class _LinksParser(HTMLParser):
    """Извлекает все <a href> со страницы расписания."""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href")
            if href:
                self.links.append((href, ""))

    def handle_data(self, data):
        if self.links:
            href, text = self.links[-1]
            self.links[-1] = (href, text + data)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _fetch(
    url: str,
    *,
    timeout: float = 30.0,
    max_retries: int = 2,
    quiet: bool = False,
) -> bytes | None:
    """
    Загружает содержимое URL с повторными попытками.

    При quiet=True ошибки (в т.ч. 404) не логируются — удобно для «прощупывания»
    возможных URL (например, PDF замен по датам), где 404 — норма.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=BROWSER_HEADERS,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < max_retries and not quiet:
                logging.warning(
                    "Повторная попытка %d/%d для %s: %s",
                    attempt + 1, max_retries, url, exc,
                )
    if not quiet:
        logging.error("Не удалось загрузить %s после %d попыток: %s", url, max_retries + 1, last_exc)
    return None


# Для обратной совместимости (используется снаружи из handlers.py опосредованно)
fetch_file = _fetch


# ── поиск ссылки на .xls ─────────────────────────────────────────────────────

async def get_latest_schedule_url() -> str | None:
    """
    Ищет ссылку на .xls/.xlsx на странице расписания.
    Если не находит — возвращает резервную прямую ссылку из конфига.
    """
    page = await _fetch(SCHEDULE_PAGE_URL)
    if page:
        parser = _LinksParser()
        parser.feed(page.decode("utf-8", errors="replace"))
        links = [
            urljoin(SCHEDULE_PAGE_URL, href)
            for href, _ in parser.links
            if re.search(r"\.xls(?:x)?(?:$|\?)", href, re.I)
        ]
        if links:
            logging.info("Найдено ссылок на .xls: %d", len(links))
            return links[0]

    # Fallback: используем прямую ссылку из конфига
    logging.warning(
        "Не удалось найти .xls на странице — использую прямую ссылку: %s",
        DIRECT_SCHEDULE_URL,
    )
    return DIRECT_SCHEDULE_URL


# ── парсинг Excel ─────────────────────────────────────────────────────────────

def _open_workbook(file_bytes: bytes):
    """Открывает .xls книгу из байтов."""
    try:
        return xlrd.open_workbook(file_contents=file_bytes, encoding_override="cp1251")
    except Exception as exc:
        raise ValueError("Файл не является читаемой книгой .xls") from exc


def _normalize(text: str) -> str:
    """Удаляет пробелы и приводит к нижнему регистру для сравнения."""
    return re.sub(r"\s+", "", text).casefold()


def _find_group(text: str, group_name: str) -> int:
    """
    Ищет group_name как отдельное слово в text (casefold, пробелы сохраняются).
    Возвращает индекс начала или -1.

    «ВЕБ-42» не должно срабатывать на «ВЕБ-42к» (другая группа) — проверяется,
    что после совпадения идёт не буква/цифра.
    """
    haystack = text.casefold()
    needle = group_name.casefold()
    idx = 0
    while True:
        idx = haystack.find(needle, idx)
        if idx == -1:
            return -1
        after = idx + len(needle)
        if after >= len(haystack) or not haystack[after].isalnum():
            return idx
        idx = after


def _extract_room(text: str, group_idx: int, group_name: str) -> str:
    """
    Извлекает аудиторию из ячейки вида «ВЕБ-42   а.112» → «112».
    Возвращает '' если аудитории в ячейке нет (напр. «Мелохаян Е.Д. ВЕБ-42»).
    """
    tail = text[group_idx + len(group_name):].strip()
    # Убираем ведущий маркер «а.» / «ауд.»
    return re.sub(r"^[аaАA]\s*\.\s*", "", tail).strip()


def _looks_like_group_ref(text: str) -> bool:
    """
    Эвристика: похоже ли содержимое ячейки на «ГРУППА   АУДИТОРИЯ»,
    а не на название предмета.
    """
    t = text.strip()
    if not t:
        return True  # пустая — не предмет
    # Если текст содержит паттерн «а.XXX» или «Ц-X» — вероятно, это аудитория
    if re.search(r"\b[аa]\s*\.\s*\d", t, re.I):
        return True
    if re.search(r"\b[ЦC]\s*[-–]\s*\d", t, re.I):
        return True
    # Если текст целиком похож на код группы + аудитория
    if re.match(r"^[А-ЯЁA-Z]+[-–]?\d+[к]?\s+", t, re.I):
        return True
    return False


def _time_to_minutes(time_str: str) -> int:
    """Переводит '08.00-09.30' в минуты от полуночи для сортировки."""
    m = re.match(r"(\d{1,2})[.:](\d{2})", time_str)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 0


def parse_excel_pairs(
    file_bytes: bytes,
    sheet_name: str | None = None,
    group_name: str = GROUP_NAME,
) -> dict:
    """
    Извлекает пары группы из недельной таблицы Excel.

    Алгоритм (v3):
    - Сканирует ВСЕ листы.
    - Строка со временем (колонка 0) задаёт временной слот пары.
    - В столбцах 1–6 (дни недели) ищет ячейку с группой как отдельным
      словом («ВЕБ-42   а.112» или «Мелохаян Е.Д. ВЕБ-42»), извлекает
      аудиторию из той же ячейки, а предмет — из строки ниже.
    - Номер пары берётся из глобальной нумерации временных слотов
      (08.00→1, 09.40→2, 11.30→3, …), поэтому день, начинающийся
      со 2-й пары, получает корректные номера.

    Возвращает {weekday_index: {pair_num: {"subject", "room", "is_replacement"}}}.
    weekday_index: 0=Пн … 5=Сб.
    """
    workbook = _open_workbook(file_bytes)
    sheets = [workbook.sheet_by_index(i) for i in range(workbook.nsheets)]

    # ── Глобальная нумерация временных слотов ──────────────────────────────
    all_times: set[str] = set()
    for sheet in sheets:
        for r in range(sheet.nrows):
            first = str(sheet.cell_value(r, 0)).strip()
            if TIME_RE.match(first):
                all_times.add(first)
    slot_num: dict[str, int] = {
        t: i + 1 for i, t in enumerate(sorted(all_times, key=_time_to_minutes))
    }

    # Сырые данные: {day_idx: [(time, subject, is_replacement, room)]}
    raw: dict[int, list[tuple[str, str, bool, str]]] = {d: [] for d in range(6)}

    for sheet in sheets:
        in_replacements = False
        r = 0
        while r < sheet.nrows:
            first = str(sheet.cell_value(r, 0)).strip()
            first_lower = first.casefold()

            # Отслеживаем секцию «Замены»
            if "замен" in first_lower:
                in_replacements = True
                r += 1
                continue

            # Выход из секции замен при встрече нового заголовка
            if in_replacements and first and not TIME_RE.match(first):
                if not any(
                    d in first_lower
                    for d in ("понедельник", "вторник", "среда",
                              "четверг", "пятница", "суббота")
                ):
                    in_replacements = False

            if not TIME_RE.match(first):
                r += 1
                continue

            # --- Найдена строка со временем ---
            time_str = first
            # Проверяем строки r, r+1, r+2 на наличие группы
            for col in range(1, min(sheet.ncols, 7)):
                for off in range(3):
                    if r + off >= sheet.nrows:
                        break
                    cell = str(sheet.cell_value(r + off, col)).strip()
                    if not cell:
                        continue
                    group_idx = _find_group(cell, group_name)
                    if group_idx < 0:
                        continue
                    room = _extract_room(cell, group_idx, group_name)
                    # Предмет — в следующей строке, тот же столбец
                    subj_row = r + off + 1
                    subject = ""
                    if subj_row < sheet.nrows:
                        subject = str(sheet.cell_value(subj_row, col)).strip()
                        if subject and _looks_like_group_ref(subject):
                            subject = ""
                    raw[col - 1].append(
                        (time_str, subject, in_replacements, room)
                    )
                    break  # нашли для этого столбца
            r += 1

    # --- Слияние дубликатов между листами и нумерация ---
    # Для каждого (день, время) берём самое полное название предмета и
    # непустую аудиторию (аудитории есть только на листе «Преподаватели»).
    schedule: dict[int, dict[int, dict]] = {d: {} for d in range(6)}
    for day_idx, entries in raw.items():
        merged: dict[str, dict] = {}
        for time_str, subject, is_repl, room in entries:
            cur = merged.get(time_str)
            if cur is None:
                merged[time_str] = {
                    "subject": subject, "room": room, "is_replacement": is_repl,
                }
                continue
            if len(subject) > len(cur["subject"]):
                cur["subject"] = subject
            if room and not cur["room"]:
                cur["room"] = room
            if is_repl:
                cur["is_replacement"] = True

        for time_str, data in sorted(
            merged.items(), key=lambda x: _time_to_minutes(x[0])
        ):
            num = slot_num.get(time_str)
            if num is None:
                continue
            schedule[day_idx][num] = data

    # Логируем результат
    for day_idx in range(6):
        count = len(schedule[day_idx])
        if count:
            parts = []
            for n in sorted(schedule[day_idx]):
                d = schedule[day_idx][n]
                repl = " [ЗАМЕНА]" if d["is_replacement"] else ""
                room = f" @{d['room']}" if d.get("room") else ""
                parts.append(f"{n}: {d['subject']}{room}{repl}")
            logging.info("%s: %d пар — %s", DAYS[day_idx], count, ", ".join(parts))
        else:
            logging.info("%s: нет пар", DAYS[day_idx])

    return schedule


# ── замены (PDF) ──────────────────────────────────────────────────────────────


_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_PAIR_TOKEN_RE = re.compile(r"(\d+)\s*п\s*(?:[-–]\s*(\d+)\s*п)?")
# Аудитория: «230», «221б», «ВЦ-4», «с/з» (спортзал), «а/к6» (акт. зал)
_ROOM_RE = re.compile(r"^(?:\d+[а-яё]?|ВЦ[-–]\d+|с/з|а/к\d+)$", re.I)
# Инициалы: «И.П.», «И.», «П.» (отдельно или слитно)
_INITIAL_RE = re.compile(r"^[А-ЯЁ]\.[А-ЯЁ]?\.?$")
# Фамилия: «Круглова», «Лозиченко» (возможен дефис)
_SURNAME_RE = re.compile(r"^[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?$")
_PDF_READER = None  # ленивый импорт pypdf.PdfReader


def _pdf_extract_text(file_bytes: bytes) -> str:
    """Извлекает весь текст из PDF (через pypdf) и возвращает строку."""
    global _PDF_READER
    if _PDF_READER is None:
        try:
            from pypdf import PdfReader
        except Exception as exc:  # noqa: BLE001
            raise ImportError(
                "Для чтения PDF-замен требуется пакет pypdf (pip install pypdf)"
            ) from exc
        _PDF_READER = PdfReader

    reader = _PDF_READER(io.BytesIO(file_bytes))
    parts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
            if text:
                parts.append(text)
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(parts)


def _parse_pair_token(pair_str: str) -> list[int]:
    """
    Разбирает поле «ПАРА»: «3п» → [3], «1п-2п» → [1, 2], «3п-5п» → [3, 4, 5].
    """
    m = _PAIR_TOKEN_RE.search(pair_str)
    if not m:
        return []
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if end < start:
        start, end = end, start
    return list(range(start, end + 1))


def _split_names(tokens: list[str]) -> list[list[str]]:
    """
    Группирует токены в имена «Фамилия Инициалы».
    «Круглова И.П. Кожаев Э.З.» → [["Круглова","И.П."], ["Кожаев","Э.З."]].
    Скобочные примечания прикрепляются к текущему имени.
    """
    names: list[list[str]] = []
    for tok in tokens:
        if _INITIAL_RE.match(tok) and names:
            names[-1].append(tok)
        elif _SURNAME_RE.match(tok):
            names.append([tok])
        else:
            if names:
                names[-1].append(tok)
            else:
                names.append([tok])
    return names


def _parse_replace_tokens(tokens: list[str]) -> tuple[str, str]:
    """
    Из токенов строки замены (после «группа пара») извлекает
    (преподаватель-замена, аудитория).

    Колонки PDF: ПРЕПОДАВАТЕЛЬ ПО РАСПИСАНИЮ | ЗАМЕНА | АУДИТОРИЯ.
    Нас интересует колонка «ЗАМЕНА» (последнее имя) и аудитория.
    """
    room = ""
    if tokens and _ROOM_RE.fullmatch(tokens[-1]):
        room = tokens[-1]
        tokens = tokens[:-1]
    # «Дополнительно» — пары не было в расписании, дальше идёт имя замены
    while tokens and tokens[0].casefold() == "дополнительно":
        tokens = tokens[1:]
    names = _split_names(tokens)
    replacement = " ".join(names[-1]) if names else " ".join(tokens)
    if replacement.casefold() in ("", "б/з"):
        replacement = ""
    return replacement, room


def parse_replacement_pdf(
    file_bytes: bytes,
    group_name: str = GROUP_NAME,
) -> dict:
    """
    Разбирает PDF-замен и возвращает замены для группы.

    Формат PDF: секции «1 курс» … «4 курс», в каждой — шапка и строки вида
    «ГРУППА  ПАРА  ПРЕПОДАВАТЕЛЬ_ПО_РАСПИСАНИЮ  ЗАМЕНА  АУДИТОРИЯ».

    Возвращает: {date_key (ISO "YYYY-MM-DD"): {номер_пары: {"teacher", "room"}}}.
    """
    text = _pdf_extract_text(file_bytes)
    target = _normalize(group_name)
    result: dict[str, dict[int, dict]] = {}
    current_date: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Строка с датой: «13.11.2026 г. (пятница) 4 курс (верхняя неделя)».
        date_match = _DATE_RE.search(line)
        if date_match:
            day, month, year = (
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
            )
            try:
                current_date = date(year, month, day).isoformat()
            except ValueError:
                current_date = None
            continue

        tokens = line.split()
        if len(tokens) < 2 or current_date is None:
            continue

        # Первый токен — группа (например «ВЕБ-42», «ВЕБ-42к» — разные группы).
        if _normalize(tokens[0]) != target:
            continue

        pair_nums = _parse_pair_token(tokens[1])
        teacher, room = _parse_replace_tokens(tokens[2:])
        day_map = result.setdefault(current_date, {})
        for num in pair_nums:
            day_map[num] = {"teacher": teacher, "room": room}

    return result


def _current_week_dates() -> list[date]:
    """Возвращает даты текущей недели (Пн … Сб)."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return [monday + timedelta(days=i) for i in range(6)]


def build_replacement_candidate_urls(day: date) -> list[str]:
    """
    Строит возможные URL PDF-замен для конкретной даты.

    Наименование на сайте: /wp-content/uploads/{ГГГГ}/{ММ}/ZAMENY-{дд}.{мм}.{гг}-g.pdf
    """
    year = f"{day.year:04d}"
    month = f"{day.month:02d}"
    stem = f"ZAMENY-{day.day:02d}.{day.month:02d}.{day.year % 100:02d}-g.pdf"
    base = urljoin(COLLEGE_URL, "wp-content/uploads/")
    return [urljoin(base, f"{year}/{month}/{stem}")]


async def get_replacement_urls() -> list[str]:
    """
    Ищет ссылки на PDF-замен на странице расписания.

    Возвращает список абсолютных URL (без повторов, в порядке появления).
    """
    page = await _fetch(SCHEDULE_PAGE_URL)
    if not page:
        return []

    parser = _LinksParser()
    parser.feed(page.decode("utf-8", errors="replace"))

    urls: list[str] = []
    seen: set[str] = set()
    for href, text in parser.links:
        if not re.search(r"\.pdf(?:$|\?)", href, re.I):
            continue
        haystack = href.casefold() + " " + text.casefold()
        # Ссылка на замены: «ZAMENY-…» (латиница) или «Замены» (кириллица)
        if "zamen" not in haystack and "замен" not in haystack:
            continue
        url = urljoin(SCHEDULE_PAGE_URL, href)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


async def get_replacements_for_group(group_name: str = GROUP_NAME) -> dict:
    """
    Загружает и разбирает PDF-замены для группы.

    Возвращает {date_key (ISO): {номер_пары: {"teacher", "room"}}}.
    Пустой словарь, если замен нет или файлы недоступны.

    Источники ссылок:
    1. Страница расписания (актуальные PDF «ZAMENY-…»).
    2. Резерв: URL по датам текущей недели («работает» в любой день/месяц).
    """
    urls = await get_replacement_urls()
    for day in _current_week_dates():
        urls.extend(build_replacement_candidate_urls(day))

    # Дедупликация с сохранением порядка
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)

    result: dict[str, dict[int, str]] = {}
    for url in unique:
        # Для PDF замен 404 — норма (нет замен на эту дату): не ретраим, не логируем
        content = await _fetch(url, max_retries=0, quiet=True)
        if not content:
            continue
        try:
            parsed = parse_replacement_pdf(content, group_name)
        except Exception:  # noqa: BLE001
            logging.exception("Не удалось разобрать PDF замен: %s", url)
            continue
        for date_key, pairs in parsed.items():
            result.setdefault(date_key, {}).update(pairs)

    if result:
        logging.info("Замены найдены для дат: %s", ", ".join(sorted(result)))
    return result


# ── главная точка входа ──────────────────────────────────────────────────────

async def get_full_week_schedule(group_name: str = GROUP_NAME) -> list:
    """
    Загружает актуальное расписание и возвращает список дней текущей недели.

    Каждый элемент списка:
        {
            "day_name": str,      # "Понедельник" … "Суббота"
            "date": str,          # "12.11"
            "full_date": str,     # "2026-11-12"
            "pairs": [
                {"num": int, "subject": str, "room": str, "is_replacement": bool},
                …
            ],
        }
    """
    # ── Mock-режим: возвращаем тестовые данные без выхода в сеть ──────────
    if USE_MOCK_SCHEDULE:
        global _MOCK_SCHEDULE_CACHE
        if _MOCK_SCHEDULE_CACHE is None:
            _MOCK_SCHEDULE_CACHE = _build_mock_week()
        logging.info("[MOCK] Возвращаю тестовое расписание")
        return _MOCK_SCHEDULE_CACHE

    # ── Реальный парсинг ──────────────────────────────────────────────────
    schedule_url = await get_latest_schedule_url()
    if not schedule_url:
        logging.error("Не удалось получить URL расписания — возвращаю пустые данные")
        main_pairs: dict = {day: {} for day in range(6)}
    else:
        file_bytes = await _fetch(schedule_url)
        if not file_bytes:
            logging.error("Не удалось скачать файл расписания по URL: %s", schedule_url)
            main_pairs = {day: {} for day in range(6)}
        else:
            try:
                main_pairs = parse_excel_pairs(file_bytes, group_name=group_name)
            except Exception:
                logging.exception("Не удалось разобрать файл расписания %s", schedule_url)
                main_pairs = {day: {} for day in range(6)}

    replacements = await get_replacements_for_group(group_name)
    start = datetime.now().date() - timedelta(days=datetime.now().weekday())

    result = []
    for i, day_name in enumerate(DAYS):
        current = start + timedelta(days=i)
        date_key = current.isoformat()
        day_replacements = replacements.get(date_key, {})
        day_pairs = main_pairs.get(i, {})

        # Собираем все номера пар: из основного расписания + из замен
        nums = sorted(set(day_pairs) | set(day_replacements))
        pairs = []
        for num in nums:
            repl_info = day_replacements.get(num)
            base = day_pairs.get(num, {})
            if repl_info is not None:
                # Заменённая пара: показываем преподавателя-замену и аудиторию
                subject = repl_info.get("teacher") or base.get("subject", "")
                room = repl_info.get("room") or base.get("room", "")
                is_replacement = True
            else:
                subject = base.get("subject", "")
                room = base.get("room", "")
                is_replacement = base.get("is_replacement", False)
            pairs.append({
                "num": num,
                "subject": subject,
                "room": room,
                "is_replacement": is_replacement,
            })
        result.append({
            "day_name": day_name,
            "date": current.strftime("%d.%m"),
            "full_date": date_key,
            "pairs": pairs,
        })
    return result