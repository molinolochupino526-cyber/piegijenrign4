"""
Вспомогательные утилиты: логирование, повторные попытки с экспоненциальной
задержкой, безопасные имена файлов и определение длительности аудио.
"""

from __future__ import annotations

import functools
import json
import logging
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable, Optional, Type, TypeVar

logger = logging.getLogger("ticket_video")

T = TypeVar("T")


# --------------------------------------------------------------------------- #
#  Логирование
# --------------------------------------------------------------------------- #
def setup_logging(level: str = "INFO", log_file: Optional[Path] = None) -> logging.Logger:
    """
    Настраивает логирование в консоль и (опционально) в файл.
    Имя потока в формате помогает различать параллельные задачи.
    """
    root = logging.getLogger("ticket_video")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(threadName)-12s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    return root


# --------------------------------------------------------------------------- #
#  Повторные попытки (retry + экспоненциальный backoff)
# --------------------------------------------------------------------------- #
class RetryableError(RuntimeError):
    """Ошибка, которую имеет смысл повторить (таймаут, 5xx, 429)."""


def retry_with_backoff(
    max_attempts: int = 4,
    base: float = 2.0,
    exceptions: tuple = (RetryableError,),
    jitter: float = 0.3,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Декоратор повторных попыток: задержки 2, 4, 8, 16 секунд (+ случайный джиттер,
    чтобы параллельные потоки не били в API одновременно).
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_error: Optional[BaseException] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:              # noqa: PERF203
                    last_error = exc
                    if attempt == max_attempts:
                        break
                    delay = base ** attempt
                    delay += random.uniform(0, jitter * delay)
                    logger.warning(
                        "Попытка %d/%d для %s не удалась (%s). Повтор через %.1f c",
                        attempt, max_attempts, func.__name__, exc, delay,
                    )
                    time.sleep(delay)
            raise RuntimeError(
                f"{func.__name__}: исчерпаны все {max_attempts} попыток"
            ) from last_error

        return wrapper

    return decorator


# --------------------------------------------------------------------------- #
#  Имена файлов
# --------------------------------------------------------------------------- #
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(text: str, max_length: int = 60) -> str:
    """
    Превращает название билета в безопасное имя файла:
    'Билет №3: Право' -> 'bilet_3_pravo'
    """
    text = text.strip().lower()
    result = []
    for char in text:
        if char in _TRANSLIT:
            result.append(_TRANSLIT[char])
        elif char.isalnum() and char.isascii():
            result.append(char)
        else:
            result.append("_")
    slug = re.sub(r"_+", "_", "".join(result)).strip("_")
    return (slug[:max_length].rstrip("_")) or "ticket"


# --------------------------------------------------------------------------- #
#  Длительность аудио
# --------------------------------------------------------------------------- #
def audio_duration(path: Path) -> float:
    """
    Возвращает длительность аудиофайла в секундах.
    Порядок попыток: mutagen (быстро, без запуска процессов) -> ffprobe -> moviepy.
    """
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        raise FileNotFoundError(f"Аудиофайл не найден или пуст: {path}")

    # 1) mutagen — самый быстрый способ
    try:
        from mutagen.mp3 import MP3

        duration = float(MP3(str(path)).info.length)
        if duration > 0:
            return duration
    except Exception as exc:  # noqa: BLE001
        logger.debug("mutagen не смог прочитать %s: %s", path.name, exc)

    # 2) ffprobe (системный или из imageio-ffmpeg)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_format", str(path)],
                capture_output=True, text=True, check=True, timeout=60,
            )
            duration = float(json.loads(out.stdout)["format"]["duration"])
            if duration > 0:
                return duration
        except Exception as exc:  # noqa: BLE001
            logger.debug("ffprobe не смог прочитать %s: %s", path.name, exc)

    # 3) moviepy как последний резерв
    try:
        from moviepy import AudioFileClip  # moviepy 2.x
    except ImportError:                      # pragma: no cover - moviepy 1.x
        from moviepy.editor import AudioFileClip  # type: ignore

    clip = AudioFileClip(str(path))
    try:
        return float(clip.duration)
    finally:
        clip.close()


def human_time(seconds: float) -> str:
    """Форматирует секунды как MM:SS (для логов)."""
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes:02d}:{secs:02d}"


def file_is_ready(path: Path, min_size: int = 1024) -> bool:
    """
    Проверяет, что файл уже сгенерирован (существует и не является обрывком).
    Используется, чтобы не переделывать работу при повторном запуске.
    """
    return path.exists() and path.stat().st_size >= min_size
