"""
Конфигурация генератора обучающих видео.

Все настройки читаются из переменных окружения (файл .env через python-dotenv),
но у каждой есть разумное значение по умолчанию, поэтому скрипт запускается
даже с пустым .env — кроме, разумеется, API-ключей для озвучки.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv

# Загружаем .env из корня проекта (переменные окружения имеют приоритет).
load_dotenv()

# Шрифты, которые пробуем по очереди, если FONT_PATH не задан явно.
# Важно: шрифт обязан содержать кириллицу, иначе текст превратится в квадратики.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Unicode.ttf",           # macOS
    "C:/Windows/Fonts/arialbd.ttf",               # Windows
)


# --------------------------------------------------------------------------- #
#  Вспомогательные функции чтения переменных окружения
# --------------------------------------------------------------------------- #
def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except ValueError:
        return default


def _hex_to_rgb(value: str, default: Tuple[int, int, int]) -> Tuple[int, int, int]:
    """Преобразует '#RRGGBB' в кортеж (R, G, B)."""
    value = value.lstrip("#").strip()
    if len(value) != 6:
        return default
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return default


def _detect_font() -> str:
    """Ищет первый доступный шрифт с кириллицей."""
    explicit = _env_str("FONT_PATH")
    if explicit and Path(explicit).exists():
        return explicit
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    # Ничего не нашли — вернём как есть, MoviePy сообщит понятную ошибку.
    return explicit or FONT_CANDIDATES[0]


# --------------------------------------------------------------------------- #
#  Секции конфигурации
# --------------------------------------------------------------------------- #
@dataclass
class TTSConfig:
    """Настройки синтеза речи."""

    provider: str = "fish"              # основной провайдер: fish | azure
    allow_fallback: bool = True         # переключаться ли на Azure при падении Fish

    # Fish Audio
    fish_api_key: str = ""
    fish_voice_id: str = ""             # reference_id голоса из библиотеки Fish Audio
    fish_model: str = "speech-1.6"
    fish_api_url: str = "https://api.fish.audio/v1/tts"

    # Azure Speech Services
    azure_key: str = ""
    azure_region: str = "westeurope"
    azure_voice: str = "ru-RU-SvetlanaNeural"
    azure_style: str = ""               # newscast / cheerful / calm; пусто — без стиля
    azure_format: str = "audio-24khz-48kbitrate-mono-mp3"

    speech_rate: str = "+8%"            # скорость речи (используется в SSML Azure)
    timeout: int = 180                  # таймаут HTTP-запроса, сек
    max_retries: int = 4                # число попыток при сетевой ошибке
    backoff_base: float = 2.0           # база экспоненциальной задержки: 2, 4, 8, 16 сек

    @property
    def azure_endpoint(self) -> str:
        return f"https://{self.azure_region}.tts.speech.microsoft.com/cognitiveservices/v1"


@dataclass
class SplitConfig:
    """Настройки разбиения билета на части."""

    target_chars: int = 3000    # целевой размер части в символах
    min_chars: int = 400        # части короче склеиваем с предыдущей


@dataclass
class VideoConfig:
    """Настройки рендера видео."""

    width: int = 1080
    height: int = 1920
    fps: int = 30

    font: str = field(default_factory=_detect_font)
    font_size_current: int = 60          # текущее предложение — крупно
    font_size_side: int = 30             # соседние предложения — мельче
    side_opacity: float = 0.5            # прозрачность соседних предложений

    text_color: Tuple[int, int, int] = (255, 255, 255)
    highlight_color: Tuple[int, int, int] = (255, 212, 0)   # жёлтая подсветка слова
    stroke_color: Tuple[int, int, int] = (0, 0, 0)
    stroke_width: int = 3                # обводка, чтобы текст читался на любом фоне

    margin_x: int = 80                   # боковые отступы
    block_center_y: float = 0.46         # вертикальный центр блока субтитров (доля высоты)
    font_size_min: int = 36              # нижняя граница при авто-уменьшении кегля
    line_spacing: int = 18               # межстрочный интервал внутри предложения
    line_height_factor: float = 1.55     # высота строки = кегль * этот коэффициент
    max_block_ratio: float = 0.42        # максимальная высота блока субтитров (доля кадра)
    side_gap: int = 46                   # отступ до соседних предложений
    side_max_chars: int = 110            # обрезка соседних предложений, чтобы не ломать вёрстку

    fade: float = 0.25                   # длительность fade in/out предложения, сек

    banner_height: int = 130             # высота нижней плашки
    banner_bottom_offset: int = 90       # отступ плашки от низа кадра
    banner_opacity: float = 0.55
    banner_font_size: int = 42
    banner_color: Tuple[int, int, int] = (0, 0, 0)

    random_bg_start: bool = True         # начинать фон со случайного места
    codec: str = "libx264"
    audio_codec: str = "aac"
    preset: str = "medium"
    bitrate: str = "6000k"
    ffmpeg_threads: int = 4


@dataclass
class AppConfig:
    """Корневая конфигурация приложения."""

    tickets_dir: Path = Path("tickets")
    output_dir: Path = Path("output")
    background_video: Path = Path("assets/background.mp4")

    tts_workers: int = 4                 # параллельные запросы к TTS
    render_workers: int = 2              # параллельный рендер видео (тяжёлый по CPU)

    srt_mode: str = "words"              # words | sentences — гранулярность SRT
    log_level: str = "INFO"
    log_file: Path = Path("logs/generator.log")

    tts: TTSConfig = field(default_factory=TTSConfig)
    split: SplitConfig = field(default_factory=SplitConfig)
    video: VideoConfig = field(default_factory=VideoConfig)

    # --- Производные пути ---------------------------------------------------
    @property
    def audio_dir(self) -> Path:
        return self.output_dir / "audio"

    @property
    def srt_dir(self) -> Path:
        return self.output_dir / "srt"

    @property
    def video_dir(self) -> Path:
        return self.output_dir / "video"

    def ensure_dirs(self) -> None:
        """Создаёт все рабочие каталоги."""
        for path in (self.audio_dir, self.srt_dir, self.video_dir, self.log_file.parent):
            path.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
#  Сборка конфигурации из окружения
# --------------------------------------------------------------------------- #
def load_config() -> AppConfig:
    """Собирает AppConfig из переменных окружения/.env."""

    tts = TTSConfig(
        provider=_env_str("TTS_PROVIDER", "fish").lower(),
        allow_fallback=_env_str("TTS_ALLOW_FALLBACK", "1") not in ("0", "false", "no"),
        fish_api_key=_env_str("FISH_API_KEY"),
        fish_voice_id=_env_str("FISH_VOICE_ID"),
        fish_model=_env_str("FISH_MODEL", "speech-1.6"),
        fish_api_url=_env_str("FISH_API_URL", "https://api.fish.audio/v1/tts"),
        azure_key=_env_str("AZURE_SPEECH_KEY"),
        azure_region=_env_str("AZURE_SPEECH_REGION", "westeurope"),
        azure_voice=_env_str("AZURE_VOICE", "ru-RU-SvetlanaNeural"),
        azure_style=_env_str("AZURE_STYLE"),
        azure_format=_env_str("AZURE_FORMAT", "audio-24khz-48kbitrate-mono-mp3"),
        speech_rate=_env_str("SPEECH_RATE", "+8%"),
        timeout=_env_int("TTS_TIMEOUT", 180),
        max_retries=_env_int("TTS_MAX_RETRIES", 4),
        backoff_base=_env_float("TTS_BACKOFF_BASE", 2.0),
    )

    split = SplitConfig(
        target_chars=_env_int("PART_TARGET_CHARS", 3000),
        min_chars=_env_int("PART_MIN_CHARS", 400),
    )

    video = VideoConfig(
        width=_env_int("VIDEO_WIDTH", 1080),
        height=_env_int("VIDEO_HEIGHT", 1920),
        fps=_env_int("VIDEO_FPS", 30),
        font=_detect_font(),
        font_size_current=_env_int("FONT_SIZE_CURRENT", 60),
        font_size_side=_env_int("FONT_SIZE_SIDE", 30),
        side_opacity=_env_float("SIDE_OPACITY", 0.5),
        text_color=_hex_to_rgb(_env_str("TEXT_COLOR", "#FFFFFF"), (255, 255, 255)),
        highlight_color=_hex_to_rgb(_env_str("HIGHLIGHT_COLOR", "#FFD400"), (255, 212, 0)),
        fade=_env_float("SENTENCE_FADE", 0.25),
        preset=_env_str("FFMPEG_PRESET", "medium"),
        bitrate=_env_str("VIDEO_BITRATE", "6000k"),
        ffmpeg_threads=_env_int("FFMPEG_THREADS", 4),
    )

    config = AppConfig(
        tickets_dir=Path(_env_str("TICKETS_DIR", "tickets")),
        output_dir=Path(_env_str("OUTPUT_DIR", "output")),
        background_video=Path(_env_str("BACKGROUND_VIDEO", "assets/background.mp4")),
        tts_workers=_env_int("TTS_WORKERS", 4),
        render_workers=_env_int("RENDER_WORKERS", 2),
        srt_mode=_env_str("SRT_MODE", "words").lower(),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        log_file=Path(_env_str("LOG_FILE", "logs/generator.log")),
        tts=tts,
        split=split,
        video=video,
    )
    return config
