from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from moviemax.cgv import CgvClient
from moviemax.config import ConfigError, Settings
from moviemax.console_config import ConsoleSettings
from moviemax.console_store import ConsoleStore
from moviemax.console_web import console_web_health, run_console_web
from moviemax.console_worker import ConsoleWorker, console_worker_health
from moviemax.locking import ProcessLock
from moviemax.service import MonitorService, healthcheck
from moviemax.state import StateStore
from moviemax.telegram import TelegramClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CGV Yongsan IMAX Telegram monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="run the continuous monitor")
    subparsers.add_parser(
        "run-once", help="run one stateful poll and deliver pending alerts"
    )
    subparsers.add_parser(
        "check-cgv", help="query CGV once without changing state or sending alerts"
    )
    subparsers.add_parser("healthcheck", help="check local heartbeat and SQLite state")
    subparsers.add_parser(
        "telegram-chat-id", help="show chats that sent the bot a message"
    )
    subparsers.add_parser("telegram-test", help="send one Telegram test message")
    subparsers.add_parser("outbox-status", help="show pending/dead notification counts")
    subparsers.add_parser("retry-dead", help="requeue dead-letter notifications")
    subparsers.add_parser("console-migrate", help="initialize the console database")
    subparsers.add_parser("console-web", help="run the MovieMax admin console")
    subparsers.add_parser("console-worker", help="run the dynamic CGV monitor worker")
    subparsers.add_parser("console-web-health", help="check the console web service")
    subparsers.add_parser("console-worker-health", help="check the console worker")
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _print_screenings(settings: Settings) -> int:
    client = CgvClient(settings)
    movie_no = client.resolve_movie_no()
    dates = client.get_screening_dates(movie_no)
    screenings = []
    for screening_date in dates:
        screenings.extend(client.get_imax_screenings(movie_no, screening_date))
    print(
        f"CGV {settings.site_name} · {settings.movie_name} · {settings.format_keyword}"
    )
    print(f"movieNo={movie_no}, dates={len(dates)}, screenings={len(screenings)}")
    for item in screenings:
        free = (
            "매진"
            if item.free_seats == 0
            else f"{item.free_seats}/{item.total_seats}석"
        )
        start = f"{item.start_time[:2]}:{item.start_time[2:]}"
        end = (
            f"{item.end_time[:2]}:{item.end_time[2:]}"
            if len(item.end_time) == 4
            else item.end_time
        )
        print(
            f"{item.screening_date} {start}-{end} {item.screen_name} {item.format_name} {free}"
        )
    if not screenings:
        print("현재 조회된 대상 IMAX 회차가 없습니다.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command.startswith("console-"):
            console_settings = ConsoleSettings.from_env()
            base_settings = console_settings.base_cgv_settings
            _configure_logging(base_settings.log_level)

            if args.command == "console-migrate":
                store = ConsoleStore(
                    console_settings.database_path,
                    console_settings.encryption_key,
                )
                if console_settings.seed_default_target:
                    target = store.ensure_default_target(
                        company_code=base_settings.company_code,
                        site_no=base_settings.site_no,
                        site_name=base_settings.site_name,
                        movie_no=base_settings.movie_no,
                        movie_name=base_settings.movie_name,
                        format_keyword=base_settings.format_keyword,
                        screen_grade_code=base_settings.screen_grade_code,
                        poll_interval_seconds=base_settings.poll_interval_seconds,
                        poll_jitter_seconds=base_settings.poll_jitter_seconds,
                    )
                    print(f"migrated target_id={target['id']}")
                else:
                    print("migrated")
                return 0
            if args.command == "console-web-health":
                print(json.dumps(console_web_health(console_settings)))
                return 0
            if args.command == "console-worker-health":
                print(json.dumps(console_worker_health(console_settings)))
                return 0
            if args.command == "console-web":
                run_console_web(console_settings)
                return 0

            stop_event = threading.Event()

            def stop_console(_signum: int, _frame: object) -> None:
                stop_event.set()

            signal.signal(signal.SIGINT, stop_console)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, stop_console)
            lock_path = console_settings.database_path.with_suffix(".worker.lock")
            with ProcessLock(lock_path):
                ConsoleWorker(
                    console_settings,
                    base_settings=base_settings,
                ).run_forever(stop_event)
            return 0

        if args.command in {"run", "run-once"}:
            settings = Settings.from_env(
                require_telegram_token=True,
                require_telegram_chat=True,
            )
        elif args.command == "telegram-chat-id":
            settings = Settings.from_env(require_telegram_token=True)
        elif args.command == "telegram-test":
            settings = Settings.from_env(
                require_telegram_token=True,
                require_telegram_chat=True,
            )
        else:
            settings = Settings.from_env()
        _configure_logging(settings.log_level)

        if args.command == "check-cgv":
            return _print_screenings(settings)
        if args.command == "healthcheck":
            healthcheck(settings)
            print("healthy")
            return 0
        if args.command == "telegram-chat-id":
            candidates = TelegramClient(
                settings.telegram_bot_token,
                timeout=settings.request_timeout_seconds,
            ).chat_candidates()
            if not candidates:
                print(
                    "조회된 채팅이 없습니다. 텔레그램에서 봇에게 /start를 보낸 뒤 다시 실행하세요."
                )
                return 1
            for candidate in candidates:
                print(
                    f"id={candidate['id']} type={candidate['type']} title={candidate['title']}"
                )
            return 0
        if args.command == "telegram-test":
            TelegramClient(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                settings.request_timeout_seconds,
            ).send_message("✅ MovieMax CGV IMAX 알림 테스트가 성공했습니다.")
            print("sent")
            return 0
        if args.command == "outbox-status":
            store = StateStore(settings.state_db_path)
            print(store.outbox_health())
            return 0
        if args.command == "retry-dead":
            with ProcessLock(settings.process_lock_path):
                count = StateStore(settings.state_db_path).requeue_dead()
            print(f"requeued={count}")
            return 0

        with ProcessLock(settings.process_lock_path):
            service = MonitorService(settings)
            if args.command == "run-once":
                result = service.poll_once()
                print(result)
                return 0

            stop_event = threading.Event()

            def stop(_signum: int, _frame: object) -> None:
                stop_event.set()

            signal.signal(signal.SIGINT, stop)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, stop)
            service.run_forever(stop_event)
            return 0
    except (ConfigError, RuntimeError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
