"""
Сборка вертикального видео 1080x1920 в MoviePy.

Что получается в кадре (сверху вниз):
    • зацикленный геймплейный фон, слегка затемнённый для читаемости;
    • предыдущее предложение — мелко и полупрозрачно;
    • ТЕКУЩЕЕ предложение — крупно, слово за словом подсвечивается жёлтым;
    • следующее предложение — мелко и полупрозрачно;
    • нижняя полупрозрачная плашка «Билет: … | Часть X из Y».

Субтитры не бегут строкой: блок предложения статичен, меняется только подсветка
слова, а смена предложений идёт через плавный fade in/out.

Модуль совместим с MoviePy 2.x (основная ветка) и 1.0.3 — различия в API
изолированы в секции «Слой совместимости».
"""

from __future__ import annotations

import logging
import math
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .config import AppConfig, VideoConfig
from .splitter import TicketPart
from .subtitles import SentenceTiming

logger = logging.getLogger("ticket_video.video")

# --------------------------------------------------------------------------- #
#  Слой совместимости MoviePy 1.x / 2.x
# --------------------------------------------------------------------------- #
try:  # MoviePy 2.x
    from moviepy import (
        AudioFileClip,
        ColorClip,
        CompositeVideoClip,
        TextClip,
        VideoFileClip,
        concatenate_videoclips,
        vfx,
    )

    MOVIEPY_V2 = True
except ImportError:  # pragma: no cover — MoviePy 1.0.3
    from moviepy.editor import (  # type: ignore
        AudioFileClip,
        ColorClip,
        CompositeVideoClip,
        TextClip,
        VideoFileClip,
        concatenate_videoclips,
    )
    import moviepy.video.fx.all as vfx  # type: ignore

    MOVIEPY_V2 = False


def _call(clip, v2_name: str, v1_name: str, *args, **kwargs):
    """Вызывает метод клипа с учётом версии MoviePy."""
    method = getattr(clip, v2_name, None) if MOVIEPY_V2 else getattr(clip, v1_name, None)
    if method is None:  # на случай промежуточных версий
        method = getattr(clip, v1_name, None) or getattr(clip, v2_name)
    return method(*args, **kwargs)


def with_start(clip, value):
    return _call(clip, "with_start", "set_start", value)


def with_duration(clip, value):
    return _call(clip, "with_duration", "set_duration", value)


def with_position(clip, value):
    return _call(clip, "with_position", "set_position", value)


def with_opacity(clip, value):
    return _call(clip, "with_opacity", "set_opacity", value)


def with_audio(clip, audio):
    return _call(clip, "with_audio", "set_audio", audio)


def subclip(clip, start, end):
    return _call(clip, "subclipped", "subclip", start, end)


def resize_clip(clip, factor):
    return _call(clip, "resized", "resize", factor)


def crop_clip(clip, **kwargs):
    return _call(clip, "cropped", "crop", **kwargs)


def without_audio(clip):
    if MOVIEPY_V2 and hasattr(clip, "without_audio"):
        return clip.without_audio()
    return _call(clip, "with_audio", "set_audio", None)


def fade_in_out(clip, fade: float):
    """Плавное появление и исчезновение клипа (по маске прозрачности)."""
    if fade <= 0:
        return clip
    try:
        if MOVIEPY_V2:
            return clip.with_effects([vfx.CrossFadeIn(fade), vfx.CrossFadeOut(fade)])
        return clip.crossfadein(fade).crossfadeout(fade)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Fade недоступен (%s) — клип остаётся без анимации", exc)
        return clip


def darken(clip, factor: float):
    """Затемняет фон, чтобы белые субтитры читались поверх яркого геймплея."""
    if factor >= 1.0:
        return clip
    try:
        if MOVIEPY_V2:
            return clip.with_effects([vfx.MultiplyColor(factor)])
        return clip.fx(vfx.colorx, factor)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Затемнение фона недоступно: %s", exc)
        return clip


def loop_clip(clip, duration: float):
    """
    Зацикливает фон до нужной длительности.
    Сначала пробуем штатный эффект Loop, иначе склеиваем копии вручную.
    """
    if clip.duration is None:
        return with_duration(clip, duration)
    if clip.duration >= duration:
        return subclip(clip, 0, duration)

    try:
        if MOVIEPY_V2:
            return clip.with_effects([vfx.Loop(duration=duration)])
        return clip.loop(duration=duration)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("vfx.Loop недоступен (%s) — склеиваем копии вручную", exc)

    repeats = int(math.ceil(duration / clip.duration))
    return subclip(concatenate_videoclips([clip] * repeats), 0, duration)


def ffmpeg_binary() -> str:
    """Путь к ffmpeg: системный или поставляемый imageio-ffmpeg."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "ffmpeg не найден. Установите ffmpeg или пакет imageio-ffmpeg."
        ) from exc


def _rgb(color: Tuple[int, int, int]) -> str:
    """(255, 212, 0) -> '#ffd400' (строку понимают обе ветки MoviePy)."""
    return "#{:02x}{:02x}{:02x}".format(*color)


# --------------------------------------------------------------------------- #
#  Текстовые клипы
# --------------------------------------------------------------------------- #
def make_text_clip(text: str, cfg: VideoConfig, font_size: int,
                   color: Tuple[int, int, int], *, method: str = "label",
                   size: Optional[Tuple[Optional[int], Optional[int]]] = None,
                   stroke: bool = True):
    """Создаёт TextClip, скрывая разницу в сигнатурах MoviePy 1.x/2.x."""
    stroke_width = cfg.stroke_width if stroke else 0
    if MOVIEPY_V2:
        # В MoviePy 2.x size — обязательный кортеж; (None, None) = размер по тексту
        size = size or (None, None)
        return TextClip(
            font=cfg.font,
            text=text,
            font_size=font_size,
            color=_rgb(color),
            stroke_color=_rgb(cfg.stroke_color) if stroke_width else None,
            stroke_width=stroke_width,
            method=method,
            size=size,
            text_align="center",
            horizontal_align="center",
            vertical_align="center",
            interline=cfg.line_spacing,
        )
    return TextClip(  # pragma: no cover — MoviePy 1.x (требует ImageMagick)
        txt=text,
        font=cfg.font,
        fontsize=font_size,
        color=_rgb(color),
        stroke_color=_rgb(cfg.stroke_color) if stroke_width else None,
        stroke_width=stroke_width,
        method=method,
        size=size,
        align="center",
    )


def _space_width(cfg: VideoConfig, font_size: int) -> int:
    """Ширина пробела в пикселях — нужна для ручной вёрстки строки по словам."""
    try:
        from PIL import ImageFont

        font = ImageFont.truetype(cfg.font, font_size)
        return max(int(round(font.getlength(" "))), font_size // 4)
    except Exception:  # noqa: BLE001
        return max(font_size // 3, 8)


@dataclass
class PlacedWord:
    """Слово, размещённое в блоке субтитров."""

    index: int          # порядковый номер слова в предложении
    text: str
    x: int
    y: int
    width: int
    height: int


@dataclass
class SentenceLayout:
    """Результат вёрстки одного предложения."""

    words: List[PlacedWord]
    height: int
    font_size: int
    cache: dict


def layout_sentence(words: Sequence[str], cfg: VideoConfig, max_width: int,
                    font_size: int) -> Tuple[List[PlacedWord], int, dict]:
    """
    Верстает предложение по словам: переносит строки по ширине и центрирует их.

    Возвращает (список размещённых слов, высота блока, кэш клипов слов).
    Кэш нужен, чтобы не рендерить одинаковые слова дважды.
    """
    space = _space_width(cfg, font_size)
    clip_cache: dict = {}

    # 1. Измеряем каждое слово (клипы сразу кладём в кэш — пригодятся при сборке)
    measured: List[Tuple[str, int, int]] = []
    for word in words:
        key = (word, font_size, "base")
        if key not in clip_cache:
            clip_cache[key] = make_text_clip(word, cfg, font_size, cfg.text_color)
        clip = clip_cache[key]
        measured.append((word, int(clip.size[0]), int(clip.size[1])))

    # 2. Жадный перенос строк
    lines: List[List[Tuple[int, str, int, int]]] = [[]]
    line_width = 0
    for idx, (word, width, height) in enumerate(measured):
        extra = width if not lines[-1] else width + space
        if lines[-1] and line_width + extra > max_width:
            lines.append([])
            line_width = 0
            extra = width
        lines[-1].append((idx, word, width, height))
        line_width += extra

    # 3. Раскладываем строки по координатам (по центру кадра)
    line_height = int(font_size * cfg.line_height_factor) + cfg.line_spacing
    placed: List[PlacedWord] = []
    for line_no, line in enumerate(lines):
        total = sum(w for _, _, w, _ in line) + space * max(len(line) - 1, 0)
        cursor_x = (cfg.width - total) // 2
        line_top = line_no * line_height
        for idx, word, width, height in line:
            placed.append(
                PlacedWord(
                    index=idx,
                    text=word,
                    x=cursor_x,
                    y=line_top + (line_height - height) // 2,
                    width=width,
                    height=height,
                )
            )
            cursor_x += width + space

    block_height = max(len(lines), 1) * line_height
    return placed, block_height, clip_cache


def layout_sentence_fit(words: Sequence[str], cfg: VideoConfig,
                        max_width: int) -> SentenceLayout:
    """
    Верстает предложение, при необходимости уменьшая кегль.

    Длинное предложение крупным шрифтом не помещается в кадр, поэтому кегль
    ступенчато снижается, пока блок не впишется в отведённую высоту
    (но не ниже cfg.font_size_min).
    """
    max_height = int(cfg.height * cfg.max_block_ratio)
    font_size = cfg.font_size_current

    while True:
        placed, height, cache = layout_sentence(words, cfg, max_width, font_size)
        # Слово шире экрана переносом не спасти — помогает только кегль поменьше
        widest = max((word.width for word in placed), default=0)
        fits = height <= max_height and widest <= max_width

        if fits or font_size <= cfg.font_size_min:
            if not fits:
                logger.debug(
                    "Предложение не вписалось даже кеглем %d "
                    "(высота %d px, самое широкое слово %d px)",
                    font_size, height, widest,
                )
            return SentenceLayout(words=placed, height=height,
                                  font_size=font_size, cache=cache)
        # Освобождаем неподошедшие клипы и пробуем кегль поменьше
        for clip in cache.values():
            try:
                clip.close()
            except Exception:  # noqa: BLE001
                pass
        font_size = max(font_size - 6, cfg.font_size_min)


def _shorten(text: str, limit: int) -> str:
    """Обрезает соседнее предложение, чтобы оно не ломало вёрстку."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------- #
#  Фон и нижний баннер
# --------------------------------------------------------------------------- #
def build_background(background_path: Path, duration: float, cfg: VideoConfig):
    """
    Готовит фон: обрезает под 9:16 «по заполнению», затемняет и зацикливает
    на всю длительность аудио.
    """
    clip = without_audio(VideoFileClip(str(background_path)))

    # Масштабируем так, чтобы кадр полностью покрыл 1080x1920, лишнее обрезаем
    scale = max(cfg.width / clip.w, cfg.height / clip.h)
    if abs(scale - 1.0) > 1e-3:
        clip = resize_clip(clip, scale)
    clip = crop_clip(
        clip,
        width=cfg.width, height=cfg.height,
        x_center=clip.w / 2, y_center=clip.h / 2,
    )

    # Случайная точка старта — чтобы все части не начинались с одного кадра
    if cfg.random_bg_start and clip.duration and clip.duration > duration + 1:
        offset = random.uniform(0, clip.duration - duration - 1)
        clip = subclip(clip, offset, clip.duration)

    clip = loop_clip(clip, duration)
    clip = darken(clip, 0.75)
    return with_duration(clip, duration)


def build_banner(caption: str, duration: float, cfg: VideoConfig) -> List:
    """Нижняя полупрозрачная плашка с названием билета и номером части."""
    top = cfg.height - cfg.banner_bottom_offset - cfg.banner_height

    plate = ColorClip(size=(cfg.width, cfg.banner_height), color=cfg.banner_color)
    plate = with_opacity(with_position(with_duration(plate, duration), (0, top)),
                         cfg.banner_opacity)

    text = make_text_clip(
        caption, cfg, cfg.banner_font_size, cfg.text_color,
        method="caption", size=(cfg.width - 2 * cfg.margin_x, cfg.banner_height),
        stroke=False,
    )
    text = with_position(with_duration(text, duration), (cfg.margin_x, top))
    return [plate, text]


# --------------------------------------------------------------------------- #
#  Субтитры
# --------------------------------------------------------------------------- #
def build_subtitle_clips(timeline: Sequence[SentenceTiming], cfg: VideoConfig) -> List:
    """
    Собирает клипы субтитров для всей части.

    Для каждого предложения:
      • белые клипы слов на всё время предложения (появляются с fade);
      • жёлтые клипы-дубликаты поверх — включаются ровно на время своего слова;
      • мелкие полупрозрачные соседние предложения сверху и снизу.
    """
    clips: List = []
    max_width = cfg.width - 2 * cfg.margin_x
    center_y = int(cfg.height * cfg.block_center_y)

    for position, sentence in enumerate(timeline):
        duration = sentence.duration
        if duration <= 0.01:
            continue

        words = [w.text for w in sentence.words] or sentence.text.split()
        layout = layout_sentence_fit(words, cfg, max_width)
        placed, cache, font_size = layout.words, layout.cache, layout.font_size
        block_height = layout.height
        block_top = center_y - block_height // 2

        # --- текущее предложение: белый слой + жёлтая подсветка ---------------
        for item in placed:
            base = cache[(item.text, font_size, "base")]
            base = with_position(
                with_duration(with_start(base, sentence.start), duration),
                (item.x, block_top + item.y),
            )
            clips.append(fade_in_out(base, cfg.fade))

            # Жёлтый дубль слова: тот же шрифт и координаты, поэтому подсветка
            # ложится пиксель в пиксель поверх белого текста.
            if item.index < len(sentence.words):
                word_timing = sentence.words[item.index]
                highlight_key = (item.text, font_size, "hl")
                if highlight_key not in cache:
                    cache[highlight_key] = make_text_clip(
                        item.text, cfg, font_size, cfg.highlight_color
                    )
                highlight = with_position(
                    with_duration(
                        with_start(cache[highlight_key], word_timing.start),
                        max(word_timing.duration, 1.0 / 30),
                    ),
                    (item.x, block_top + item.y),
                )
                clips.append(highlight)

        # --- соседние предложения (мелко, полупрозрачно) ----------------------
        side_width = max_width
        if position > 0:
            previous = make_text_clip(
                _shorten(timeline[position - 1].text, cfg.side_max_chars),
                cfg, cfg.font_size_side, cfg.text_color,
                method="caption", size=(side_width, None), stroke=False,
            )
            previous = with_position(
                with_duration(with_start(previous, sentence.start), duration),
                (cfg.margin_x, block_top - cfg.side_gap - previous.size[1]),
            )
            clips.append(fade_in_out(with_opacity(previous, cfg.side_opacity), cfg.fade))

        if position + 1 < len(timeline):
            following = make_text_clip(
                _shorten(timeline[position + 1].text, cfg.side_max_chars),
                cfg, cfg.font_size_side, cfg.text_color,
                method="caption", size=(side_width, None), stroke=False,
            )
            following = with_position(
                with_duration(with_start(following, sentence.start), duration),
                (cfg.margin_x, block_top + block_height + cfg.side_gap),
            )
            clips.append(fade_in_out(with_opacity(following, cfg.side_opacity), cfg.fade))

    return clips


# --------------------------------------------------------------------------- #
#  Рендер части
# --------------------------------------------------------------------------- #
def render_part(part: TicketPart, audio_path: Path, timeline: Sequence[SentenceTiming],
                background_path: Path, output_path: Path, config: AppConfig,
                force: bool = False) -> Path:
    """
    Собирает и кодирует финальное видео одной части.

    :return: путь к готовому MP4
    """
    from .utils import file_is_ready, human_time

    output_path = Path(output_path)
    if not force and file_is_ready(output_path, min_size=50_000):
        logger.info("Видео уже существует, пропускаем: %s", output_path.name)
        return output_path

    cfg = config.video
    audio = AudioFileClip(str(audio_path))
    duration = float(audio.duration)
    logger.info("Рендер %s: %s аудио, %d предложений",
                output_path.name, human_time(duration), len(timeline))

    background = build_background(background_path, duration, cfg)
    subtitle_clips = build_subtitle_clips(timeline, cfg)
    banner_clips = build_banner(part.caption, duration, cfg)

    composite = CompositeVideoClip(
        [background, *subtitle_clips, *banner_clips],
        size=(cfg.width, cfg.height),
    )
    composite = with_audio(with_duration(composite, duration), audio)

    # Пишем во временный файл и переименовываем — прерванный рендер
    # не оставит «готовый» битый MP4.
    tmp_output = output_path.with_suffix(".part.mp4")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        composite.write_videofile(
            str(tmp_output),
            fps=cfg.fps,
            codec=cfg.codec,
            audio_codec=cfg.audio_codec,
            bitrate=cfg.bitrate,
            preset=cfg.preset,
            threads=cfg.ffmpeg_threads,
            temp_audiofile=str(output_path.with_suffix(".temp-audio.m4a")),
            remove_temp=True,
            logger=None,          # прогресс-бар мешает при параллельном рендере
        )
        tmp_output.replace(output_path)
    finally:
        # Аккуратно освобождаем ffmpeg-ридеры, иначе при пакетной обработке
        # процесс упирается в лимит открытых файлов.
        for clip in (composite, background, audio, *subtitle_clips, *banner_clips):
            try:
                clip.close()
            except Exception:  # noqa: BLE001
                pass
        if tmp_output.exists():
            tmp_output.unlink(missing_ok=True)

    logger.info("Готово: %s (%.1f МБ)",
                output_path.name, output_path.stat().st_size / 1024 / 1024)
    return output_path
