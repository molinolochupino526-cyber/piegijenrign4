"""
Построение таймлайна субтитров и генерация SRT.

Точных word-level таймкодов TTS-провайдеры в MP3 не отдают, поэтому используется
аппроксимация: общая длительность аудио распределяется между предложениями и
словами пропорционально их «весу» (длине в символах + штраф за знаки препинания,
на которых диктор делает паузу). На практике такая оценка даёт рассинхрон в
пределах десятых долей секунды, что для караоке-подсветки достаточно.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Sequence

logger = logging.getLogger("ticket_video.subtitles")

# Дополнительный «вес» (в условных символах) для пауз на знаках препинания.
PAUSE_WEIGHTS = {
    ",": 1.5, ";": 2.5, ":": 2.5, "—": 2.0, "-": 0.5,
    ".": 4.0, "!": 4.0, "?": 4.0, "…": 5.0,
}


@dataclass
class WordTiming:
    """Одно слово с таймкодами."""

    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)


@dataclass
class SentenceTiming:
    """Предложение с таймкодами и разбивкой по словам."""

    index: int
    text: str
    start: float
    end: float
    words: List[WordTiming] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)


def _word_weight(word: str) -> float:
    """Вес слова = длина + штраф за знаки препинания в конце."""
    weight = float(len(word))
    for char in word[-3:]:
        weight += PAUSE_WEIGHTS.get(char, 0.0)
    # Цифры произносятся дольше букв ('2024' -> «две тысячи двадцать четыре»)
    weight += 2.5 * sum(char.isdigit() for char in word)
    return max(weight, 1.0)


def build_timeline(sentences: Sequence[str], audio_duration: float,
                   lead_in: float = 0.0) -> List[SentenceTiming]:
    """
    Распределяет длительность аудио по предложениям и словам.

    :param sentences: список предложений части
    :param audio_duration: длительность MP3 из TTS в секундах
    :param lead_in: пауза в начале (если диктор «разгоняется»), сек
    :return: список SentenceTiming со сквозными таймкодами
    """
    sentences = [s for s in sentences if s.strip()]
    if not sentences or audio_duration <= 0:
        return []

    # Вес каждого предложения — сумма весов его слов
    sentence_words: List[List[str]] = [s.split() for s in sentences]
    sentence_weights = [
        sum(_word_weight(w) for w in words) or 1.0 for words in sentence_words
    ]
    total_weight = sum(sentence_weights)

    usable = max(audio_duration - lead_in, 0.1)
    cursor = lead_in
    timeline: List[SentenceTiming] = []

    for idx, (text, words, weight) in enumerate(
        zip(sentences, sentence_words, sentence_weights)
    ):
        sentence_duration = usable * weight / total_weight
        sentence_start = cursor
        sentence_end = cursor + sentence_duration

        # Внутри предложения распределяем время по словам
        word_timings: List[WordTiming] = []
        word_weights = [_word_weight(w) for w in words] or [1.0]
        words_total = sum(word_weights)
        word_cursor = sentence_start
        for word, w_weight in zip(words, word_weights):
            word_duration = sentence_duration * w_weight / words_total
            word_timings.append(
                WordTiming(text=word, start=word_cursor,
                           end=word_cursor + word_duration)
            )
            word_cursor += word_duration

        # Подгоняем последнее слово точно к концу предложения (убираем дрейф)
        if word_timings:
            word_timings[-1].end = sentence_end

        timeline.append(
            SentenceTiming(index=idx, text=text, start=sentence_start,
                           end=sentence_end, words=word_timings)
        )
        cursor = sentence_end

    # Последнее предложение обязано заканчиваться вместе с аудио
    if timeline:
        timeline[-1].end = audio_duration
        if timeline[-1].words:
            timeline[-1].words[-1].end = audio_duration

    return timeline


# --------------------------------------------------------------------------- #
#  SRT
# --------------------------------------------------------------------------- #
def format_timestamp(seconds: float) -> str:
    """Секунды -> 'ЧЧ:ММ:СС,мс' (формат SRT)."""
    seconds = max(float(seconds), 0.0)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:               # округление вверх до следующей секунды
        millis, secs = 0, secs + 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def timeline_to_srt(timeline: Sequence[SentenceTiming], mode: str = "words") -> str:
    """
    Собирает содержимое SRT-файла.

    mode='sentences' — один субтитр на предложение (удобно для внешних плееров);
    mode='words'     — один субтитр на слово (word-level, для караоке-подсветки).
    """
    blocks: List[str] = []
    counter = 1

    if mode == "sentences":
        for sentence in timeline:
            blocks.append(
                f"{counter}\n"
                f"{format_timestamp(sentence.start)} --> {format_timestamp(sentence.end)}\n"
                f"{sentence.text}\n"
            )
            counter += 1
    else:
        for sentence in timeline:
            for word in sentence.words:
                blocks.append(
                    f"{counter}\n"
                    f"{format_timestamp(word.start)} --> {format_timestamp(word.end)}\n"
                    f"{word.text}\n"
                )
                counter += 1

    return "\n".join(blocks)


def write_srt(path: Path, timeline: Sequence[SentenceTiming],
              mode: str = "words") -> Path:
    """Сохраняет SRT на диск (UTF-8, как принято для субтитров)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(timeline_to_srt(timeline, mode=mode), encoding="utf-8")
    logger.debug("SRT сохранён: %s (%d предложений)", path.name, len(timeline))
    return path


def parse_srt(path: Path) -> List[WordTiming]:
    """
    Читает SRT обратно (например, если таймкоды получены внешним
    инструментом вроде WhisperX и положены рядом с аудио).
    """
    content = Path(path).read_text(encoding="utf-8")
    pattern = re.compile(
        r"\d+\s*\n(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n(.+?)(?=\n\s*\n|\Z)",
        re.DOTALL,
    )
    entries: List[WordTiming] = []
    for match in pattern.finditer(content):
        h1, m1, s1, ms1, h2, m2, s2, ms2, text = match.groups()
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
        entries.append(WordTiming(text=" ".join(text.split()), start=start, end=end))
    return entries
