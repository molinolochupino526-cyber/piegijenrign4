#!/usr/bin/env python3
"""
Генератор обучающих вертикальных видео из Markdown-билетов.

Примеры запуска
---------------
    # Всё: озвучить и собрать видео по всем билетам из каталога tickets/
    python generate.py

    # Один билет, только первая часть, без обращения к TTS (проверить вёрстку)
    python generate.py --only bilet_1 --part 1 --dry-run

    # Резать по объёму (~3000 символов), а не по авторским маркерам «ЧАСТЬ N»
    python generate.py --split-mode chars

    # Только озвучка и SRT, без рендера
    python generate.py --audio-only

    # Посмотреть, на сколько частей разобьются билеты, ничего не создавая
    python generate.py --plan
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ticket_video.config import load_config
from ticket_video.pipeline import process_tickets
from ticket_video.splitter import find_tickets, load_ticket
from ticket_video.utils import setup_logging


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Генерация обучающих видео 9:16 из Markdown-билетов",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # --- пути ---------------------------------------------------------------
    parser.add_argument("--tickets", type=Path,
                        help="каталог с MD-файлами билетов")
    parser.add_argument("--output", type=Path,
                        help="каталог для результатов (audio/, srt/, video/)")
    parser.add_argument("--background", type=Path,
                        help="фоновое вертикальное видео (геймплей)")

    # --- выбор материала ----------------------------------------------------
    parser.add_argument("--only", type=str,
                        help="обработать только билеты, чьё имя содержит подстроку")
    parser.add_argument("--part", type=int, nargs="+", dest="parts",
                        help="номера частей (например: --part 1 2)")
    parser.add_argument("--split-mode", choices=("auto", "chars", "markers"),
                        default="auto",
                        help="auto (по умолчанию): использовать маркеры «ЧАСТЬ N», "
                             "если они есть; chars: всегда резать по объёму")

    # --- озвучка ------------------------------------------------------------
    parser.add_argument("--provider", choices=("fish", "azure"),
                        help="провайдер TTS (по умолчанию из .env)")
    parser.add_argument("--voice", type=str,
                        help="ID/имя голоса: reference_id для Fish Audio "
                             "или имя голоса для Azure")
    parser.add_argument("--speech-rate", type=str,
                        help="скорость речи для Azure, например +10%%")
    parser.add_argument("--no-fallback", action="store_true",
                        help="не переключаться на Azure при ошибке Fish Audio")

    # --- режимы работы ------------------------------------------------------
    parser.add_argument("--force", action="store_true",
                        help="перегенерировать даже уже готовые файлы")
    parser.add_argument("--dry-run", action="store_true",
                        help="без обращения к TTS: подставить тишину "
                             "(быстрая проверка вёрстки субтитров)")
    parser.add_argument("--audio-only", action="store_true",
                        help="остановиться после озвучки и SRT")
    parser.add_argument("--plan", action="store_true",
                        help="только показать разбиение на части и выйти")

    # --- производительность и прочее ---------------------------------------
    parser.add_argument("--workers", type=int,
                        help="число параллельных запросов к TTS")
    parser.add_argument("--render-workers", type=int,
                        help="число параллельных рендеров видео")
    parser.add_argument("--srt-mode", choices=("words", "sentences"),
                        help="гранулярность SRT: по словам или по предложениям")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="уровень логирования")

    return parser.parse_args(argv)


def apply_overrides(config, args: argparse.Namespace):
    """Аргументы командной строки перекрывают значения из .env."""
    if args.tickets:
        config.tickets_dir = args.tickets
    if args.output:
        config.output_dir = args.output
    if args.background:
        config.background_video = args.background
    if args.workers:
        config.tts_workers = args.workers
    if args.render_workers:
        config.render_workers = args.render_workers
    if args.srt_mode:
        config.srt_mode = args.srt_mode
    if args.log_level:
        config.log_level = args.log_level

    if args.provider:
        config.tts.provider = args.provider
    if args.no_fallback:
        config.tts.allow_fallback = False
    if args.speech_rate:
        config.tts.speech_rate = args.speech_rate
    if args.voice:
        # Голос подставляем тому провайдеру, который выбран
        if config.tts.provider == "azure":
            config.tts.azure_voice = args.voice
        else:
            config.tts.fish_voice_id = args.voice
    return config


def show_plan(config, args) -> int:
    """Печатает предполагаемое разбиение билетов на части без генерации."""
    files = find_tickets(config.tickets_dir)
    if args.only:
        files = [f for f in files if args.only.lower() in f.name.lower()]
    if not files:
        print(f"MD-файлы не найдены в {config.tickets_dir}")
        return 1

    for file in files:
        ticket = load_ticket(file, config.split, split_mode=args.split_mode)
        print(f"\n{file.name} -> «{ticket.title}» ({len(ticket.parts)} частей)")
        for part in ticket.parts:
            approx = len(part.text) / 14.5    # оценка длительности озвучки
            print(f"   {part.short_caption:<60} "
                  f"{len(part.text):>5} симв., {len(part.sentences):>3} предл., "
                  f"~{approx / 60:.1f} мин -> {part.base_name}_final.mp4")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    config = apply_overrides(load_config(), args)
    setup_logging(config.log_level, config.log_file)

    if args.plan:
        return show_plan(config, args)

    try:
        jobs = process_tickets(
            config,
            split_mode=args.split_mode,
            only=args.only,
            parts=args.parts,
            force=args.force,
            dry_run=args.dry_run,
            audio_only=args.audio_only,
        )
    except FileNotFoundError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 130

    # Ненулевой код возврата, если хоть одна часть не собралась —
    # удобно для запуска по расписанию/в CI
    return 0 if all(job.ok for job in jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
