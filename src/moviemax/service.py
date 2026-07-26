from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from moviemax.cgv import CgvClient
from moviemax.config import Settings
from moviemax.models import Screening
from moviemax.state import StateStore
from moviemax.telegram import TelegramClient, TelegramError, render_event_messages

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PollResult:
    dates: int
    screenings: int
    new_screenings: int
    booking_opened: int
    seat_increases: int
    notifications_sent: int
    notification_failures: int
    dead_letters: int


class MonitorService:
    def __init__(
        self,
        settings: Settings,
        *,
        cgv: CgvClient | None = None,
        store: StateStore | None = None,
        telegram: TelegramClient | None = None,
    ) -> None:
        self.settings = settings
        self.cgv = cgv or CgvClient(settings)
        self.store = store or StateStore(settings.state_db_path)
        self.telegram = telegram or TelegramClient(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            settings.request_timeout_seconds,
        )

    def fetch_screenings(self) -> tuple[list[str], list[Screening]]:
        movie_no = self.cgv.resolve_movie_no()
        dates = self.cgv.get_screening_dates(movie_no)
        screenings: list[Screening] = []
        for index, screening_date in enumerate(dates):
            if index and self.settings.request_gap_seconds:
                time.sleep(self.settings.request_gap_seconds)
            screenings.extend(self.cgv.get_screenings(movie_no, screening_date))
        return dates, screenings

    def poll_once(self) -> PollResult:
        pre_sent, pre_failed, pre_dead = self.deliver_pending()
        dates, screenings = self.fetch_screenings()
        summary = self.store.apply_snapshot(
            screenings,
            notify_on_initial_state=self.settings.notify_on_initial_state,
        )
        post_sent, post_failed, post_dead = self.deliver_pending()
        result = PollResult(
            dates=len(dates),
            screenings=summary.screening_count,
            new_screenings=summary.new_screening_count,
            booking_opened=summary.booking_opened_count,
            seat_increases=summary.seat_increase_count,
            notifications_sent=pre_sent + post_sent,
            notification_failures=pre_failed + post_failed,
            dead_letters=pre_dead + post_dead,
        )
        status = (
            "degraded" if result.notification_failures or result.dead_letters else "ok"
        )
        self.write_heartbeat(status, asdict(result))
        return result

    def deliver_pending(self) -> tuple[int, int, int]:
        sent = 0
        failed = 0
        dead = 0
        for event in self.store.pending_events():
            try:
                messages = render_event_messages(event)
            except Exception as exc:  # noqa: BLE001 - malformed persisted events must be isolated
                safe_error = f"{type(exc).__name__}: {exc}"
                self.store.mark_dead(event.id, safe_error)
                logger.error(
                    "Notification event %s was dead-lettered: %s", event.id, safe_error
                )
                failed += 1
                dead += 1
                continue

            try:
                for index in range(event.delivered_parts, len(messages)):
                    self.telegram.send_message(messages[index])
                    self.store.mark_part_delivered(event.id, index + 1)
            except TelegramError as exc:
                failed += 1
                attempts_after_failure = event.attempts + 1
                safe_error = f"{type(exc).__name__}: {exc}"
                if (
                    not exc.retryable
                    or attempts_after_failure >= self.settings.telegram_max_attempts
                ):
                    self.store.mark_dead(event.id, safe_error)
                    dead += 1
                    logger.error(
                        "Telegram event %s was dead-lettered: %s", event.id, safe_error
                    )
                    continue
                exponential = self.settings.telegram_retry_base_seconds * (
                    2 ** min(event.attempts, 6)
                )
                retry_after = max(exc.retry_after_seconds or 0, exponential)
                self.store.mark_failed(event.id, safe_error, retry_after)
                logger.error(
                    "Telegram event %s failed; retrying in %d seconds: %s",
                    event.id,
                    retry_after,
                    safe_error,
                )
                break
            except Exception as exc:  # noqa: BLE001 - unknown sender failures are retried safely
                failed += 1
                safe_error = f"{type(exc).__name__}: {exc}"
                attempts_after_failure = event.attempts + 1
                if attempts_after_failure >= self.settings.telegram_max_attempts:
                    self.store.mark_dead(event.id, safe_error)
                    dead += 1
                    continue
                retry_after = self.settings.telegram_retry_base_seconds * (
                    2 ** min(event.attempts, 6)
                )
                self.store.mark_failed(event.id, safe_error, retry_after)
                break
            else:
                self.store.mark_sent(event.id)
                sent += 1
        return sent, failed, dead

    def write_heartbeat(self, status: str, details: dict[str, object]) -> None:
        path = self.settings.heartbeat_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "status": status,
            "details": details,
        }
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def run_forever(self, stop_event: threading.Event) -> None:
        failures = 0
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                result = self.poll_once()
            except Exception as exc:  # noqa: BLE001 - the worker must back off after any poll failure
                failures += 1
                safe_error = f"{type(exc).__name__}: {exc}"
                logger.error("Poll failed (%d consecutive): %s", failures, safe_error)
                self.write_heartbeat(
                    "error",
                    {"consecutive_failures": failures, "error": safe_error},
                )
                base_delay = min(
                    self.settings.backoff_max_seconds,
                    self.settings.poll_interval_seconds * (2 ** min(failures - 1, 6)),
                )
                delay = base_delay + random.uniform(
                    0,
                    self.settings.poll_jitter_seconds,
                )
            else:
                failures = 0
                elapsed = time.monotonic() - started
                delay = max(0.0, self.settings.poll_interval_seconds - elapsed)
                delay += random.uniform(0, self.settings.poll_jitter_seconds)
                logger.info(
                    "Poll complete: dates=%d screenings=%d new=%d opened=%d "
                    "seat_increases=%d sent=%d failed=%d dead=%d",
                    result.dates,
                    result.screenings,
                    result.new_screenings,
                    result.booking_opened,
                    result.seat_increases,
                    result.notifications_sent,
                    result.notification_failures,
                    result.dead_letters,
                )
            stop_event.wait(delay)


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"{label} timestamp is invalid") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def healthcheck(settings: Settings) -> None:
    if not settings.state_db_path.is_file():
        raise RuntimeError("State database is missing")
    store = StateStore(settings.state_db_path)
    store.health_check()

    try:
        payload = json.loads(settings.heartbeat_path.read_text(encoding="utf-8"))
        heartbeat = _parse_timestamp(str(payload["timestamp"]), "Heartbeat")
        status = str(payload["status"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Heartbeat is missing or invalid") from exc
    if status not in {"ok", "degraded", "error"}:
        raise RuntimeError("Heartbeat status is invalid")
    if status == "error":
        raise RuntimeError("The latest monitor poll failed")

    now = datetime.now(UTC)
    heartbeat_age = (now - heartbeat).total_seconds()
    if heartbeat_age > settings.health_max_age_seconds:
        raise RuntimeError(f"Heartbeat is stale ({int(heartbeat_age)} seconds)")

    last_success_value = store.get_metadata("last_success_at")
    if not last_success_value:
        raise RuntimeError("No successful CGV poll has completed")
    last_success = _parse_timestamp(last_success_value, "Last successful poll")
    success_age = (now - last_success).total_seconds()
    if success_age > settings.health_max_age_seconds:
        raise RuntimeError(
            f"Last successful CGV poll is stale ({int(success_age)} seconds)"
        )

    outbox = store.outbox_health()
    if outbox["dead"]:
        raise RuntimeError(
            f"Notification outbox has {outbox['dead']} dead-letter event(s)"
        )
    if outbox["max_attempts"] >= settings.notification_health_failure_threshold:
        raise RuntimeError(
            f"Notification delivery has failed {outbox['max_attempts']} consecutive attempt(s)"
        )
