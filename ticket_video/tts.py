"""
Синтез речи: основной провайдер Fish Audio, резервный — Azure Speech.

Оба провайдера возвращают готовый MP3, который сохраняется рядом с будущим видео.
Сетевые ошибки, 429 (rate limit) и 5xx повторяются с экспоненциальной задержкой;
ошибки конфигурации (401/403/400) не повторяются — смысла нет.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

import requests

from .config import TTSConfig
from .utils import RetryableError, audio_duration, file_is_ready, retry_with_backoff

logger = logging.getLogger("ticket_video.tts")

# Коды, при которых повторять запрос осмысленно
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class TTSConfigurationError(RuntimeError):
    """Не хватает ключа/голоса — повторять бессмысленно."""


@dataclass
class TTSResult:
    """Результат озвучки одной части."""

    path: Path
    duration: float
    provider: str
    cached: bool = False


# --------------------------------------------------------------------------- #
#  Fish Audio
# --------------------------------------------------------------------------- #
def _fish_request(text: str, cfg: TTSConfig) -> bytes:
    """Один запрос к Fish Audio. Возвращает байты MP3."""
    if not cfg.fish_api_key:
        raise TTSConfigurationError("FISH_API_KEY не задан в .env")

    headers = {
        "Authorization": f"Bearer {cfg.fish_api_key}",
        "Content-Type": "application/json",
        # Заголовок model выбирает движок синтеза (speech-1.5 / speech-1.6 / s1)
        "model": cfg.fish_model,
    }
    payload = {
        "text": text,
        "format": "mp3",
        "mp3_bitrate": 128,
        "normalize": True,      # нормализация текста (числа, сокращения)
        "latency": "normal",    # 'normal' — качество выше, чем у 'balanced'
    }
    # reference_id — идентификатор голоса из библиотеки Fish Audio.
    # Если не указан, используется голос по умолчанию для аккаунта.
    if cfg.fish_voice_id:
        payload["reference_id"] = cfg.fish_voice_id

    try:
        response = requests.post(
            cfg.fish_api_url, headers=headers, json=payload, timeout=cfg.timeout
        )
    except requests.RequestException as exc:
        raise RetryableError(f"Fish Audio: сетевая ошибка — {exc}") from exc

    if response.status_code in RETRYABLE_STATUS:
        raise RetryableError(
            f"Fish Audio: HTTP {response.status_code} — {response.text[:200]}"
        )
    if response.status_code in (401, 403):
        raise TTSConfigurationError(
            f"Fish Audio: доступ запрещён (HTTP {response.status_code}). "
            f"Проверьте FISH_API_KEY. Ответ: {response.text[:200]}"
        )
    if not response.ok:
        raise RuntimeError(
            f"Fish Audio: HTTP {response.status_code} — {response.text[:300]}"
        )
    if not response.content:
        raise RetryableError("Fish Audio: пустой ответ")

    return response.content


# --------------------------------------------------------------------------- #
#  Azure Speech (fallback)
# --------------------------------------------------------------------------- #
def _build_ssml(text: str, cfg: TTSConfig) -> str:
    """Собирает SSML для Azure с нужным голосом, стилем и скоростью речи."""
    safe_text = xml_escape(text)
    inner = f'<prosody rate="{cfg.speech_rate}">{safe_text}</prosody>'
    if cfg.azure_style:
        inner = (
            f'<mstts:express-as style="{cfg.azure_style}" styledegree="1">'
            f"{inner}</mstts:express-as>"
        )
    lang = cfg.azure_voice.split("-")[0] + "-" + cfg.azure_voice.split("-")[1] \
        if cfg.azure_voice.count("-") >= 2 else "ru-RU"
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="{lang}">'
        f'<voice name="{cfg.azure_voice}">{inner}</voice></speak>'
    )


def _azure_request(text: str, cfg: TTSConfig) -> bytes:
    """Один запрос к Azure Speech. Возвращает байты MP3."""
    if not cfg.azure_key:
        raise TTSConfigurationError("AZURE_SPEECH_KEY не задан в .env")

    headers = {
        "Ocp-Apim-Subscription-Key": cfg.azure_key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": cfg.azure_format,
        "User-Agent": "ticket-video-generator",
    }
    try:
        response = requests.post(
            cfg.azure_endpoint,
            headers=headers,
            data=_build_ssml(text, cfg).encode("utf-8"),
            timeout=cfg.timeout,
        )
    except requests.RequestException as exc:
        raise RetryableError(f"Azure TTS: сетевая ошибка — {exc}") from exc

    if response.status_code in RETRYABLE_STATUS:
        raise RetryableError(
            f"Azure TTS: HTTP {response.status_code} — {response.text[:200]}"
        )
    if response.status_code in (401, 403):
        raise TTSConfigurationError(
            f"Azure TTS: доступ запрещён (HTTP {response.status_code}). "
            f"Проверьте AZURE_SPEECH_KEY и AZURE_SPEECH_REGION."
        )
    if not response.ok:
        raise RuntimeError(
            f"Azure TTS: HTTP {response.status_code} — {response.text[:300]}"
        )
    if not response.content:
        raise RetryableError("Azure TTS: пустой ответ")

    return response.content


# --------------------------------------------------------------------------- #
#  Публичный интерфейс
# --------------------------------------------------------------------------- #
def _save_atomic(data: bytes, destination: Path) -> None:
    """
    Пишет файл атомарно: сначала во временный, потом переименовывает.
    Так прерванная генерация не оставит битый MP3, который скрипт
    при следующем запуске примет за готовый.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, suffix=".part", delete=False
    ) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(destination)


def synthesize(text: str, output_path: Path, cfg: TTSConfig,
               force: bool = False) -> TTSResult:
    """
    Озвучивает текст и сохраняет MP3 в output_path.

    Если файл уже существует и не пуст — повторно API не дёргаем
    (экономия квоты при перезапуске пайплайна).

    :param force: True — перегенерировать даже при наличии файла
    """
    output_path = Path(output_path)

    if not force and file_is_ready(output_path):
        duration = audio_duration(output_path)
        logger.info("Аудио уже готово, пропускаем: %s (%.1f c)",
                    output_path.name, duration)
        return TTSResult(path=output_path, duration=duration,
                         provider="cache", cached=True)

    text = text.strip()
    if not text:
        raise ValueError("Пустой текст для озвучки")
    if len(text) > 8000:
        logger.warning("Часть очень длинная (%d символов) — возможен отказ API",
                       len(text))

    # Повторные попытки настраиваются из конфига
    fish_call = retry_with_backoff(cfg.max_retries, cfg.backoff_base)(_fish_request)
    azure_call = retry_with_backoff(cfg.max_retries, cfg.backoff_base)(_azure_request)

    providers = []
    if cfg.provider == "azure":
        providers.append(("azure", azure_call))
    else:
        providers.append(("fish", fish_call))
        if cfg.allow_fallback:
            providers.append(("azure", azure_call))

    errors = []
    for name, call in providers:
        try:
            logger.info("Озвучка через %s: %s (%d символов)",
                        name, output_path.name, len(text))
            audio_bytes = call(text, cfg)
            _save_atomic(audio_bytes, output_path)
            duration = audio_duration(output_path)
            logger.info("Готово: %s — %.1f c, %.0f КБ",
                        output_path.name, duration, len(audio_bytes) / 1024)
            return TTSResult(path=output_path, duration=duration, provider=name)
        except TTSConfigurationError as exc:
            logger.warning("Провайдер %s не настроен: %s", name, exc)
            errors.append(f"{name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.error("Провайдер %s не справился: %s", name, exc)
            errors.append(f"{name}: {exc}")

    raise RuntimeError(
        "Не удалось озвучить часть "
        f"{output_path.stem}. Ошибки: " + " | ".join(errors)
    )


def make_silent_audio(duration: float, output_path: Path,
                      force: bool = False) -> TTSResult:
    """
    Создаёт тишину нужной длительности (режим --dry-run / отладка вёрстки
    без расхода квоты API). Требует ffmpeg.

    Как и настоящая озвучка, уже готовый файл не переделывает.
    """
    import subprocess

    from .video import ffmpeg_binary

    output_path = Path(output_path)
    if not force and file_is_ready(output_path, min_size=256):
        existing = audio_duration(output_path)
        logger.info("Тишина уже готова, пропускаем: %s (%.1f c)",
                    output_path.name, existing)
        return TTSResult(path=output_path, duration=existing,
                         provider="silence", cached=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_binary(), "-y", "-f", "lavfi", "-i",
         "anullsrc=channel_layout=mono:sample_rate=24000",
         "-t", str(duration), "-q:a", "9", str(output_path)],
        check=True, capture_output=True,
    )
    return TTSResult(path=output_path, duration=duration, provider="silence")
