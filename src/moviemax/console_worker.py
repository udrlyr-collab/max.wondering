from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from moviemax.cgv import CgvClient
from moviemax.config import Settings
from moviemax.console_config import ConsoleSettings
from moviemax.console_store import ConsoleStore, StaleVersionError
from moviemax.models import Screening
from moviemax.polling import jittered_delay_seconds
from moviemax.telegram import TelegramClient, TelegramError, render_event_messages

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


class ConsoleWorker:
    def __init__(
        self,
        console_settings: ConsoleSettings,
        *,
        base_settings: Settings | None = None,
        store: ConsoleStore | None = None,
    ) -> None:
        self.console_settings = console_settings
        self.base_settings = base_settings or console_settings.base_cgv_settings
        self.store = store or ConsoleStore(
            console_settings.database_path,
            console_settings.encryption_key,
        )

    def seed(self) -> None:
        if self.console_settings.seed_default_target:
            self.store.ensure_default_target(
                company_code=self.base_settings.company_code,
                site_no=self.base_settings.site_no,
                site_name=self.base_settings.site_name,
                movie_no=self.base_settings.movie_no,
                movie_name=self.base_settings.movie_name,
                format_keyword=self.base_settings.format_keyword,
                screen_grade_code=self.base_settings.screen_grade_code,
                poll_interval_seconds=self.base_settings.poll_interval_seconds,
                poll_jitter_seconds=self.base_settings.poll_jitter_seconds,
            )

    def _client_for_target(self, target: dict[str, Any]) -> CgvClient:
        settings = replace(
            self.base_settings,
            company_code=str(target["company_code"]),
            site_no=str(target["site_no"]),
            site_name=str(target["site_name"]),
            movie_no=str(target["movie_no"]),
            movie_name=str(target["movie_name"]),
            format_keyword=str(target["format_keyword"]),
            screen_grade_code=str(target["screen_grade_code"]),
        )
        return CgvClient(settings)

    def fetch_target(self, target: dict[str, Any]) -> list[Screening]:
        client = self._client_for_target(target)
        movie_no = str(target["movie_no"])
        dates = client.get_screening_dates(movie_no)
        screenings: list[Screening] = []
        for index, screening_date in enumerate(dates):
            if index and self.base_settings.request_gap_seconds:
                time.sleep(self.base_settings.request_gap_seconds)
            screenings.extend(client.get_imax_screenings(movie_no, screening_date))
        return screenings

    def process_target(self, target: dict[str, Any]) -> dict[str, Any] | None:
        target_id = int(target["id"])
        version = int(target["version"])
        try:
            self.store.mark_target_started(target_id, expected_version=version)
            screenings = self.fetch_target(target)
            summary = self.store.apply_snapshot(target_id, version, screenings)
            self.store.mark_target_success(target_id, expected_version=version)
            logger.info(
                "Target poll complete: id=%d site=%s movie=%s screenings=%d "
                "new=%d opened=%d seat_increases=%d seat_decreases=%d",
                target_id,
                target["site_no"],
                target["movie_no"],
                summary["screening_count"],
                summary["new_screening_count"],
                summary["booking_opened_count"],
                summary["seat_increase_count"],
                summary["seat_decrease_count"],
            )
            return summary
        except StaleVersionError:
            self.store.release_stale_target(target_id)
            logger.info("Discarded stale poll result for target %d", target_id)
            return None
        except Exception as exc:  # noqa: BLE001 - target failures are isolated
            safe_error = _safe_error(exc)
            failures = int(target.get("consecutive_failures") or 0) + 1
            base_delay = min(
                self.base_settings.backoff_max_seconds,
                int(target["poll_interval_seconds"]) * (2 ** min(failures - 1, 6)),
            )
            delay = jittered_delay_seconds(
                base_delay,
                int(target["poll_jitter_seconds"]),
            )
            try:
                self.store.mark_target_failure(
                    target_id,
                    safe_error,
                    _now() + timedelta(seconds=delay),
                    expected_version=version,
                )
            except StaleVersionError:
                self.store.release_stale_target(target_id)
                logger.info(
                    "Target %d changed while its failed poll completed", target_id
                )
                return None
            logger.error("Target %d poll failed: %s", target_id, safe_error)
            return None

    def _telegram_config(self) -> dict[str, Any] | None:
        config = self.store.get_telegram_config(include_token=True)
        if config is None or not config["enabled"]:
            return None
        return config

    def deliver_pending(self) -> tuple[int, int, int]:
        config = self._telegram_config()
        if config is None:
            return 0, 0, 0

        client = TelegramClient(
            str(config["bot_token"]),
            str(config["chat_id"]),
            self.base_settings.request_timeout_seconds,
        )
        config_version = int(config["version"])
        sent = 0
        failed = 0
        dead = 0

        for event in self.store.pending_events():
            latest = self._telegram_config()
            if latest is None:
                break
            if int(latest["version"]) != config_version:
                config = latest
                config_version = int(config["version"])
                client = TelegramClient(
                    str(config["bot_token"]),
                    str(config["chat_id"]),
                    self.base_settings.request_timeout_seconds,
                )

            try:
                messages = render_event_messages(event)
            except Exception as exc:  # noqa: BLE001 - invalid persisted events are isolated
                safe_error = _safe_error(exc)
                self.store.mark_dead(event.id, safe_error)
                logger.error(
                    "Console event %s cannot be rendered: %s", event.id, safe_error
                )
                failed += 1
                dead += 1
                continue

            try:
                for index in range(event.delivered_parts, len(messages)):
                    client.send_message(messages[index])
                    self.store.mark_part_delivered(event.id, index + 1)
            except TelegramError as exc:
                failed += 1
                safe_error = _safe_error(exc)
                attempts = event.attempts + 1
                if (
                    not exc.retryable
                    or attempts >= self.base_settings.telegram_max_attempts
                ):
                    self.store.mark_dead(event.id, safe_error)
                    dead += 1
                    logger.error(
                        "Console event %s was dead-lettered: %s", event.id, safe_error
                    )
                    continue
                exponential = self.base_settings.telegram_retry_base_seconds * (
                    2 ** min(event.attempts, 6)
                )
                retry_after = max(exc.retry_after_seconds or 0, exponential)
                self.store.mark_failed(event.id, safe_error, retry_after)
                logger.error(
                    "Console event %s failed; retrying in %d seconds: %s",
                    event.id,
                    retry_after,
                    safe_error,
                )
                break
            except Exception as exc:  # noqa: BLE001 - unknown send failures are retried
                failed += 1
                safe_error = _safe_error(exc)
                attempts = event.attempts + 1
                if attempts >= self.base_settings.telegram_max_attempts:
                    self.store.mark_dead(event.id, safe_error)
                    dead += 1
                    continue
                retry_after = self.base_settings.telegram_retry_base_seconds * (
                    2 ** min(event.attempts, 6)
                )
                self.store.mark_failed(event.id, safe_error, retry_after)
                break
            else:
                self.store.mark_sent(event.id)
                sent += 1

        return sent, failed, dead

    def heartbeat(self) -> dict[str, Any]:
        targets = self.store.list_targets()
        errors = sum(1 for target in targets if target["last_error"])
        dead = len(self.store.list_outbox(status="dead", limit=1))
        payload = {
            "timestamp": _now().isoformat(),
            "status": "degraded" if errors or dead else "ok",
            "targets": len(targets),
            "target_errors": errors,
            "dead_letters": dead,
        }
        self.store.set_metadata(
            "console_worker_heartbeat",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        return payload

    def run_forever(self, stop_event: threading.Event) -> None:
        self.seed()
        last_heartbeat = 0.0
        while not stop_event.is_set():
            self.deliver_pending()
            for target in self.store.due_targets(limit=10):
                if stop_event.is_set():
                    break
                self.process_target(target)
                self.deliver_pending()

            if time.monotonic() - last_heartbeat >= 10:
                self.heartbeat()
                last_heartbeat = time.monotonic()
            stop_event.wait(self.console_settings.worker_tick_seconds)


def console_worker_health(settings: ConsoleSettings) -> dict[str, Any]:
    if not settings.database_path.is_file():
        raise RuntimeError("Console database is missing")
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    database = store.health_check()
    raw = store.get_metadata("console_worker_heartbeat")
    if raw is None:
        raise RuntimeError("Console worker heartbeat is missing")
    try:
        heartbeat = json.loads(raw)
        timestamp = datetime.fromisoformat(str(heartbeat["timestamp"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Console worker heartbeat is invalid") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age = (_now() - timestamp.astimezone(UTC)).total_seconds()
    if age > 60:
        raise RuntimeError(f"Console worker heartbeat is stale ({int(age)} seconds)")

    unhealthy = [
        target
        for target in store.list_targets()
        if target["enabled"] and int(target["consecutive_failures"]) >= 3
    ]
    if unhealthy:
        raise RuntimeError(f"{len(unhealthy)} target(s) have repeated CGV failures")
    if store.list_outbox(status="dead", limit=1):
        raise RuntimeError("Console notification outbox has dead-letter events")
    return {"database": database, "heartbeat": heartbeat, "age_seconds": age}
