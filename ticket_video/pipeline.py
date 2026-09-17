"""
Оркестрация пайплайна: текст -> озвучка -> SRT -> видео.

Этапы разнесены сознательно:
  1) TTS всех частей параллельно (упирается в сеть — потоков можно больше);
  2) рендер видео параллельно, но меньшим числом потоков (упирается в CPU).

Каждый этап пропускает уже готовые файлы, поэтому прерванный прогон можно
просто перезапустить — работа продолжится с места остановки.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import AppConfig
from .splitter import Ticket, TicketPart, find_tickets, load_ticket
from .subtitles import SentenceTiming, build_timeline, write_srt
from .tts import TTSResult, make_silent_audio, synthesize
from .utils import human_time

logger = logging.getLogger("ticket_video.pipeline")

# Средняя скорость русской речи для оценки длительности в режиме --dry-run
CHARS_PER_SECOND = 14.5


@dataclass
class PartJob:
    """Всё, что нужно знать об одной части в ходе обработки."""

    part: TicketPart
    audio_path: Path
    srt_path: Path
    video_path: Path
    audio: Optional[TTSResult] = None
    timeline: List[SentenceTiming] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def build_jobs(ticket: Ticket, config: AppConfig) -> List[PartJob]:
    """Формирует список задач с путями к файлам для каждой части билета."""
    jobs = []
    for part in ticket.parts:
        jobs.append(
            PartJob(
                part=part,
                audio_path=config.audio_dir / f"{part.base_name}.mp3",
                srt_path=config.srt_dir / f"{part.base_name}.srt",
                video_path=config.video_dir / f"{part.base_name}_final.mp4",
            )
        )
    return jobs


# --------------------------------------------------------------------------- #
#  Этап 1. Озвучка
# --------------------------------------------------------------------------- #
def run_tts_stage(jobs: Sequence[PartJob], config: AppConfig, *,
                  force: bool = False, dry_run: bool = False) -> None:
    """Параллельно озвучивает все части (или делает тишину в режиме --dry-run)."""

    def worker(job: PartJob) -> PartJob:
        try:
            if dry_run:
                # Оценка длительности по объёму текста — API не дёргаем
                duration = max(len(job.part.text) / CHARS_PER_SECOND, 3.0)
                job.audio = make_silent_audio(duration, job.audio_path, force=force)
                if not job.audio.cached:
                    logger.info("[dry-run] тишина %s для %s",
                                human_time(job.audio.duration), job.audio_path.name)
            else:
                job.audio = synthesize(job.part.text, job.audio_path,
                                       config.tts, force=force)
        except Exception as exc:  # noqa: BLE001
            job.error = f"озвучка: {exc}"
            logger.error("Не удалось озвучить %s: %s", job.part.base_name, exc)
        return job

    logger.info("Этап 1/3 — озвучка: %d частей в %d потоков",
                len(jobs), config.tts_workers)
    with ThreadPoolExecutor(max_workers=config.tts_workers,
                            thread_name_prefix="tts") as pool:
        futures = [pool.submit(worker, job) for job in jobs]
        for future in as_completed(futures):
            future.result()   # исключения уже перехвачены внутри worker


# --------------------------------------------------------------------------- #
#  Этап 2. Таймлайн и SRT
# --------------------------------------------------------------------------- #
def run_srt_stage(jobs: Sequence[PartJob], config: AppConfig) -> None:
    """
    Строит таймкоды слов и сохраняет SRT.
    Этап дешёвый (чистые вычисления), поэтому идёт последовательно.
    """
    logger.info("Этап 2/3 — субтитры: раскладка слов по времени аудио")
    for job in jobs:
        if not job.ok or job.audio is None:
            continue
        try:
            job.timeline = build_timeline(job.part.sentences, job.audio.duration)
            write_srt(job.srt_path, job.timeline, mode=config.srt_mode)
            words = sum(len(s.words) for s in job.timeline)
            logger.info("SRT: %s — %d предложений, %d слов на %s",
                        job.srt_path.name, len(job.timeline), words,
                        human_time(job.audio.duration))
        except Exception as exc:  # noqa: BLE001
            job.error = f"субтитры: {exc}"
            logger.error("Не удалось построить субтитры для %s: %s",
                         job.part.base_name, exc)


# --------------------------------------------------------------------------- #
#  Этап 3. Рендер видео
# --------------------------------------------------------------------------- #
def run_video_stage(jobs: Sequence[PartJob], config: AppConfig, *,
                    force: bool = False) -> None:
    """Параллельно рендерит видео. Потоков меньше — кодирование грузит CPU."""
    from .video import render_part   # импорт здесь: moviepy тяжёлый, грузим по факту

    background = Path(config.background_video)
    if not background.exists():
        raise FileNotFoundError(
            f"Фоновое видео не найдено: {background}. "
            f"Положите вертикальный геймплей по этому пути "
            f"или укажите другой через --background / BACKGROUND_VIDEO в .env"
        )

    def worker(job: PartJob) -> PartJob:
        try:
            render_part(job.part, job.audio_path, job.timeline, background,
                        job.video_path, config, force=force)
        except Exception as exc:  # noqa: BLE001
            job.error = f"рендер: {exc}"
            logger.exception("Не удалось отрендерить %s: %s",
                             job.part.base_name, exc)
        return job

    pending = [job for job in jobs if job.ok and job.timeline]
    logger.info("Этап 3/3 — рендер видео: %d частей в %d потоков",
                len(pending), config.render_workers)
    with ThreadPoolExecutor(max_workers=config.render_workers,
                            thread_name_prefix="render") as pool:
        futures = [pool.submit(worker, job) for job in pending]
        for future in as_completed(futures):
            future.result()


# --------------------------------------------------------------------------- #
#  Полный прогон
# --------------------------------------------------------------------------- #
def process_tickets(config: AppConfig, *, split_mode: str = "auto",
                    only: Optional[str] = None, parts: Optional[Sequence[int]] = None,
                    force: bool = False, dry_run: bool = False,
                    audio_only: bool = False) -> List[PartJob]:
    """
    Обрабатывает все билеты из каталога.

    :param only: подстрока имени файла — обработать только совпавшие билеты
    :param parts: номера частей (1-based) — например, только [1, 2]
    :param force: перегенерировать даже готовые файлы
    :param dry_run: не обращаться к TTS, подставить тишину
    :param audio_only: остановиться после озвучки и SRT
    """
    started = time.time()
    config.ensure_dirs()

    files = find_tickets(config.tickets_dir)
    if only:
        files = [f for f in files if only.lower() in f.name.lower()]
    if not files:
        raise FileNotFoundError(
            f"В каталоге {config.tickets_dir} не найдено MD-файлов"
            + (f" по фильтру «{only}»" if only else "")
        )

    logger.info("Найдено билетов: %d", len(files))

    all_jobs: List[PartJob] = []
    for file in files:
        ticket = load_ticket(file, config.split, split_mode=split_mode)
        jobs = build_jobs(ticket, config)
        if parts:
            wanted = set(parts)
            jobs = [job for job in jobs if job.part.index in wanted]
        all_jobs.extend(jobs)

    if not all_jobs:
        logger.warning("Нечего обрабатывать — проверьте фильтры --only/--part")
        return []

    run_tts_stage(all_jobs, config, force=force, dry_run=dry_run)
    run_srt_stage(all_jobs, config)
    if not audio_only:
        run_video_stage(all_jobs, config, force=force)

    # --- итоговый отчёт -----------------------------------------------------
    ok = [job for job in all_jobs if job.ok]
    failed = [job for job in all_jobs if not job.ok]
    total_audio = sum(job.audio.duration for job in ok if job.audio)

    logger.info("=" * 70)
    logger.info("Готово за %s. Успешно: %d из %d частей, суммарно %s материала",
                human_time(time.time() - started), len(ok), len(all_jobs),
                human_time(total_audio))
    for job in failed:
        logger.error("  ✗ %s — %s", job.part.base_name, job.error)
    if ok and not audio_only:
        logger.info("Видео сохранены в %s", config.video_dir.resolve())
    logger.info("=" * 70)

    return all_jobs
