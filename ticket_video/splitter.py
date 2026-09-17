"""
Чтение Markdown-билетов, очистка разметки, разбиение текста на предложения
и на части примерно по 3000 символов (по границам предложений).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Sequence

from .utils import slugify

logger = logging.getLogger("ticket_video.splitter")

# Сокращения, после точки в которых предложение НЕ заканчивается.
# Предложения длиннее этого порога разбиваются по запятым: иначе крупный
# текст не помещается на экран телефона целиком.
MAX_SENTENCE_CHARS = 200

ABBREVIATIONS = {
    "т", "тт", "др", "пр", "см", "ср", "напр", "рис", "табл", "гл", "ст", "стр",
    "п", "пп", "ч", "р", "руб", "коп", "г", "гг", "в", "вв", "им", "обл", "респ",
    "акад", "проф", "доц", "канд", "д", "мин", "макс", "тыс", "млн", "млрд",
    "ул", "просп", "корп", "кв", "им", "е", "н", "э", "etc", "vs", "ок",
}

# Символы конца предложения (включая многоточие) с возможными кавычками/скобками.
_SENTENCE_END_RE = re.compile(r"([.!?…]+[\"»”')\]]*)(\s+)")

# Маркер явно размеченной части: 'ЧАСТЬ 1', '## Часть 2:', 'PART 3'.
# Автор билета может заранее разбить текст сам — тогда мы уважаем его разметку.
PART_MARKER_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*{0,2})(?:ЧАСТЬ|Часть|PART|Part)[ \t]*[№#]?[ \t]*\d+"
    r"[ \t]*[.:)\-—]?[ \t]*(?:\*{0,2})[ \t]*$",
    re.MULTILINE,
)

# Строка вида 'Билет 1. Название' — запасной источник названия билета.
TICKET_TITLE_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*{0,2})Билет[ \t]*[№#]?[ \t]*(\d+)[ \t]*[.:)\-—][ \t]*(.+?)[ \t]*(?:\*{0,2})[ \t]*$",
    re.MULTILINE,
)


# --------------------------------------------------------------------------- #
#  Очистка Markdown
# --------------------------------------------------------------------------- #
def clean_markdown(text: str) -> str:
    """
    Убирает разметку Markdown, оставляя чистый текст для озвучки и субтитров.
    Блоки кода и таблицы удаляются целиком — читать их вслух бессмысленно.
    """
    # Строки-маркеры частей ('ЧАСТЬ 1') — служебная разметка, читать вслух не нужно
    text = PART_MARKER_RE.sub(" ", text)
    # Блоки кода ```...```
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    # HTML-комментарии
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    # HTML-теги
    text = re.sub(r"<[^>]+>", " ", text)
    # Изображения ![alt](url) — удаляем полностью
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    # Ссылки [текст](url) -> текст
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # Заголовки: '## Тема' -> 'Тема.' (точка нужна, чтобы заголовок стал предложением)
    text = re.sub(
        r"^#{1,6}\s*(.+?)\s*$",
        lambda m: m.group(1) if m.group(1).rstrip().endswith((".", "!", "?", ":")) else m.group(1) + ".",
        text,
        flags=re.MULTILINE,
    )
    # Горизонтальные линии и строки таблиц-разделителей
    text = re.sub(r"^\s*([-*_]\s*){3,}$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|[-:\s|]+\|\s*$", " ", text, flags=re.MULTILINE)
    # Вертикальные разделители таблиц
    text = text.replace("|", " ")
    # Цитаты и маркеры списков в начале строки
    text = re.sub(r"^\s*>+\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    # Жирный/курсив/зачёркнутый/инлайн-код
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{2,3}([^_]+)_{2,3}", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # Схлопываем пробелы и переносы
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def extract_title(raw_text: str, fallback: str) -> str:
    """
    Определяет название билета, по убыванию приоритета:
      1. первый заголовок Markdown '# Название';
      2. строка вида 'Билет 1. Название' -> '1. Название';
      3. первая содержательная строка файла;
      4. имя файла.
    """
    heading = re.search(r"^#\s+(.+?)\s*$", raw_text, flags=re.MULTILINE)
    if heading:
        return re.sub(r"[*_`#]", "", heading.group(1)).strip().rstrip(".")

    ticket_line = TICKET_TITLE_RE.search(raw_text)
    if ticket_line:
        number, name = ticket_line.group(1), ticket_line.group(2)
        name = re.sub(r"[*_`#]", "", name).strip().rstrip(".")
        return f"{number}. {name}"

    for line in raw_text.splitlines():
        line = line.strip()
        if line and not PART_MARKER_RE.match(line):
            return re.sub(r"[*_`#]", "", line).strip().rstrip(".")[:120]

    return fallback


def split_by_markers(raw_text: str) -> List[str]:
    """
    Делит исходный текст по маркерам 'ЧАСТЬ N', если автор разметил билет вручную.
    Возвращает список фрагментов (без строк-маркеров); пустой список — маркеров нет.
    """
    markers = list(PART_MARKER_RE.finditer(raw_text))
    if len(markers) < 2:
        return []

    chunks: List[str] = []
    # Текст до первого маркера (шапка билета) прикрепляем к первой части
    preamble = raw_text[:markers[0].start()].strip()
    for i, marker in enumerate(markers):
        start = marker.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(raw_text)
        chunk = raw_text[start:end].strip()
        if i == 0 and preamble:
            chunk = f"{preamble}\n\n{chunk}"
        if chunk:
            chunks.append(chunk)
    return chunks


# --------------------------------------------------------------------------- #
#  Разбиение на предложения
# --------------------------------------------------------------------------- #
def _is_false_boundary(text: str, end_pos: int, next_pos: int) -> bool:
    """
    Проверяет, что найденная точка — не конец предложения:
    сокращение ('и т.д.'), инициал ('А. С. Пушкин'), номер пункта ('1. Текст')
    или следующее слово со строчной буквы.
    """
    before = text[:end_pos]
    # Последнее «слово» перед знаком препинания
    word_match = re.search(r"([\w]+)[\"»”')\]]*[.!?…]+[\"»”')\]]*$", before)
    if word_match:
        word = word_match.group(1).lower()
        if word in ABBREVIATIONS:
            return True
        # Инициал: одна заглавная буква с точкой
        if len(word) == 1 and word_match.group(1).isalpha() and word_match.group(1).isupper():
            return True
        # Номер пункта списка: '1.', '12.'
        if word.isdigit() and len(word) <= 3 and before.rstrip().endswith("."):
            return True

    # Следующее слово начинается со строчной буквы — предложение продолжается
    tail = text[next_pos:next_pos + 3].lstrip()
    if tail and tail[0].isalpha() and tail[0].islower():
        return True
    return False


def split_sentences(text: str) -> List[str]:
    """Разбивает текст на предложения, аккуратно обходя сокращения."""
    text = text.strip()
    if not text:
        return []

    sentences: List[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        end_pos = match.end(1)      # позиция сразу после знака препинания
        next_pos = match.end(2)     # позиция начала следующего предложения
        if _is_false_boundary(text, end_pos, next_pos):
            continue
        sentence = text[start:end_pos].strip()
        if sentence:
            sentences.append(sentence)
        start = next_pos

    tail = text[start:].strip()
    if tail:
        sentences.append(tail)

    # Очень длинные «предложения» (текст без точек) режем по запятым,
    # иначе они не поместятся на экран.
    result: List[str] = []
    for sentence in sentences:
        if len(sentence) <= MAX_SENTENCE_CHARS:
            result.append(sentence)
        else:
            result.extend(_split_long_sentence(sentence, limit=MAX_SENTENCE_CHARS))
    return result


def _split_long_sentence(sentence: str, limit: int = 200) -> List[str]:
    """Делит слишком длинное предложение по запятым/точкам с запятой."""
    chunks: List[str] = []
    current = ""
    for piece in re.split(r"(?<=[,;:])\s+", sentence):
        if current and len(current) + len(piece) + 1 > limit:
            chunks.append(current.strip())
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        chunks.append(current.strip())
    return chunks or [sentence]


# --------------------------------------------------------------------------- #
#  Структуры данных
# --------------------------------------------------------------------------- #
@dataclass
class TicketPart:
    """Одна часть билета — единица работы: озвучка + рендер видео."""

    ticket_title: str            # 'Право собственности'
    ticket_slug: str             # 'pravo_sobstvennosti'
    index: int                   # номер части, начиная с 1
    total: int                   # всего частей в билете
    sentences: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Текст части для отправки в TTS."""
        return " ".join(self.sentences)

    @property
    def base_name(self) -> str:
        """Базовое имя файлов: 'pravo_sobstvennosti_part_1'."""
        return f"{self.ticket_slug}_part_{self.index}"

    @property
    def caption(self) -> str:
        """Подпись для нижнего баннера и заголовка части."""
        return f"Билет: {self.ticket_title} | Часть {self.index} из {self.total}"

    @property
    def short_caption(self) -> str:
        """Короткий вариант: 'Билет: Название | Часть 1/5'."""
        return f"Билет: {self.ticket_title} | Часть {self.index}/{self.total}"


@dataclass
class Ticket:
    """Билет целиком."""

    title: str
    slug: str
    source: Path
    parts: List[TicketPart] = field(default_factory=list)


# --------------------------------------------------------------------------- #
#  Разбиение на части
# --------------------------------------------------------------------------- #
def split_into_parts(sentences: Sequence[str], target_chars: int = 3000,
                     min_chars: int = 400) -> List[List[str]]:
    """
    Группирует предложения в части примерно по target_chars символов.

    Сначала считаем, сколько частей получится (округляя общий объём к целому),
    затем распределяем предложения равномерно — так последняя часть не выходит
    неестественно короткой.
    """
    if not sentences:
        return []

    total_chars = sum(len(s) + 1 for s in sentences)
    n_parts = max(1, round(total_chars / max(target_chars, 1)))
    effective_target = total_chars / n_parts

    parts: List[List[str]] = []
    current: List[str] = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence) + 1
        parts_left = n_parts - len(parts)
        # Закрываем часть, если она уже набрала свой объём и впереди есть ещё части
        if current and parts_left > 1 and current_len + sentence_len / 2 >= effective_target:
            parts.append(current)
            current, current_len = [], 0
        current.append(sentence)
        current_len += sentence_len

    if current:
        parts.append(current)

    # Если хвост получился слишком коротким — приклеиваем его к предыдущей части
    if len(parts) > 1 and sum(len(s) for s in parts[-1]) < min_chars:
        parts[-2].extend(parts.pop())

    return parts


def load_ticket(path: Path, config_split: SplitConfig,
                split_mode: str = "auto") -> Ticket:
    """
    Читает MD-файл и возвращает билет, разбитый на части.

    split_mode:
      'auto'    — если в файле есть маркеры 'ЧАСТЬ N', используем их,
                  иначе режем по ~target_chars символов (по границам предложений);
      'chars'   — всегда резать по объёму, маркеры игнорировать;
      'markers' — резать только по маркерам (если их нет — откат к 'chars').
    """
    raw = Path(path).read_text(encoding="utf-8")
    title = extract_title(raw, fallback=Path(path).stem)
    slug = slugify(title)

    marker_chunks = split_by_markers(raw)
    use_markers = bool(marker_chunks) and split_mode in ("auto", "markers")

    if use_markers:
        # Каждый размеченный автором фрагмент — отдельная часть
        groups = [split_sentences(clean_markdown(chunk)) for chunk in marker_chunks]
        groups = [g for g in groups if g]
        source_mode = f"маркеры «ЧАСТЬ» ({len(groups)} шт.)"
    else:
        if marker_chunks and split_mode == "chars":
            logger.info("Маркеры частей найдены, но split_mode=chars — режем по объёму")
        body = clean_markdown(raw)
        sentences = split_sentences(body)
        groups = split_into_parts(sentences, config_split.target_chars,
                                  config_split.min_chars)
        source_mode = f"по ~{config_split.target_chars} символов"

    ticket = Ticket(title=title, slug=slug, source=Path(path))
    total = len(groups)
    ticket.parts = [
        TicketPart(ticket_title=title, ticket_slug=slug, index=i + 1,
                   total=total, sentences=group)
        for i, group in enumerate(groups)
    ]

    logger.info(
        "Билет «%s»: %d частей (%s), объём частей: %s",
        title, total, source_mode,
        ", ".join(str(len(p.text)) for p in ticket.parts),
    )
    if marker_chunks and split_mode == "auto":
        logger.debug("Использована авторская разметка частей; для резки по объёму "
                     "запустите с --split-mode chars")
    return ticket


def find_tickets(tickets_dir: Path) -> List[Path]:
    """Находит все Markdown-файлы билетов, отсортированные по имени."""
    return sorted(
        p for p in Path(tickets_dir).glob("*.md") if p.is_file()
    )
