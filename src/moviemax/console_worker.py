from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from moviemax.cgv import CgvClient
from moviemax.config import Settings
from moviemax.console_config import ConsoleSettings
from moviemax.console_store import ConsoleStore, StaleVersionError
from moviemax.locking import BlockingFileLock
from moviemax.models import Screening
from moviemax.polling import jittered_delay_seconds
from moviemax.telegram import TelegramClient, TelegramError, render_event_messages
from moviemax.web_push import WebPushClient, WebPushError, render_web_push_payload

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
                format_code=self.base_settings.format_code,
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
            format_code=str(target["format_code"]),
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
            latest = self.store.get_target(int(target["id"]))
            if latest is None:
                raise KeyError(f"target {target['id']} does not exist")
            if int(latest["version"]) != int(target["version"]):
                raise StaleVersionError("target version is stale")
            screenings.extend(client.get_screenings(movie_no, screening_date))
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
        except (StaleVersionError, KeyError):
            self.store.release_stale_target(target_id)
            logger.info(
                "Discarded stale or deleted poll result for target %d", target_id
            )
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
            except (StaleVersionError, KeyError):
                self.store.release_stale_target(target_id)
                logger.info(
                    "Target %d changed or was deleted while its failed poll completed",
                    target_id,
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
        events = self.store.pending_events()
        for event in events:
            if event.telegram_chat_id:
                continue
            try:
                self.store.mark_telegram_skipped(event.id)
            except KeyError:
                continue

        config = self._telegram_config()
        if config is None:
            return 0, 0, 0

        config_version = int(config["version"])
        clients: dict[str, TelegramClient] = {}
        sent = 0
        failed = 0
        dead = 0

        for event in events:
            if not event.telegram_chat_id:
                continue
            if not self.store.is_outbox_event_pending(event.id):
                continue
            latest = self._telegram_config()
            if latest is None:
                break
            if int(latest["version"]) != config_version:
                config = latest
                config_version = int(config["version"])
                clients.clear()

            chat_id = event.telegram_chat_id
            if not chat_id:
                continue
            client = clients.get(chat_id)
            if client is None:
                client = TelegramClient(
                    str(config["bot_token"]),
                    chat_id,
                    self.base_settings.request_timeout_seconds,
                )
                clients[chat_id] = client

            try:
                messages = render_event_messages(event)
            except Exception as exc:  # noqa: BLE001 - invalid persisted events are isolated
                safe_error = _safe_error(exc)
                try:
                    self.store.mark_dead(event.id, safe_error)
                except KeyError:
                    continue
                logger.error(
                    "Console event %s cannot be rendered: %s", event.id, safe_error
                )
                failed += 1
                dead += 1
                continue

            try:
                for index in range(event.delivered_parts, len(messages)):
                    with BlockingFileLock(self.store.dispatch_lock_path):
                        if not self.store.is_outbox_event_pending(event.id):
                            break
                        client.send_message(messages[index])
                        try:
                            self.store.mark_part_delivered(event.id, index + 1)
                        except KeyError:
                            break
                if not self.store.is_outbox_event_pending(event.id):
                    continue
            except TelegramError as exc:
                failed += 1
                safe_error = _safe_error(exc)
                attempts = event.attempts + 1
                if (
                    not exc.retryable
                    or attempts >= self.base_settings.telegram_max_attempts
                ):
                    try:
                        self.store.mark_dead(event.id, safe_error)
                    except KeyError:
                        continue
                    dead += 1
                    logger.error(
                        "Console event %s was dead-lettered: %s", event.id, safe_error
                    )
                    continue
                exponential = self.base_settings.telegram_retry_base_seconds * (
                    2 ** min(event.attempts, 6)
                )
                retry_after = max(exc.retry_after_seconds or 0, exponential)
                try:
                    self.store.mark_failed(event.id, safe_error, retry_after)
                except KeyError:
                    continue
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
                    try:
                        self.store.mark_dead(event.id, safe_error)
                    except KeyError:
                        continue
                    dead += 1
                    continue
                retry_after = self.base_settings.telegram_retry_base_seconds * (
                    2 ** min(event.attempts, 6)
                )
                try:
                    self.store.mark_failed(event.id, safe_error, retry_after)
                except KeyError:
                    continue
                break
            else:
                try:
                    self.store.mark_sent(event.id)
                except KeyError:
                    continue
                else:
                    sent += 1

        return sent, failed, dead

    def deliver_pending_web_push(self) -> tuple[int, int, int]:
        # Keep notification transport failures from consuming the CGV polling
        # loop. One dispatch performs at most one external Push request.
        deliveries = self.store.pending_web_push_deliveries(limit=1)
        if not deliveries:
            return 0, 0, 0
        vapid = self.store.get_web_push_vapid(include_private=True)
        client = WebPushClient(
            str(vapid["private_key"]),
            self.console_settings.public_origin,
            self.base_settings.request_timeout_seconds,
        )
        sent = 0
        failed = 0
        dead = 0
        for delivery in deliveries:
            if not self.store.is_web_push_delivery_pending(delivery.id):
                continue
            try:
                payload = render_web_push_payload(
                    delivery.event,
                    fallback_url=self.console_settings.public_origin,
                )
            except Exception as exc:  # noqa: BLE001 - persisted payload is isolated
                safe_error = _safe_error(exc)
                if self.store.mark_web_push_dead(delivery.id, safe_error):
                    failed += 1
                    dead += 1
                continue
            with BlockingFileLock(self.store.dispatch_lock_path):
                if not self.store.is_web_push_delivery_pending(delivery.id):
                    continue
                try:
                    client.send(delivery.subscription_info, payload)
                except WebPushError as exc:
                    failed += 1
                    safe_error = _safe_error(exc)
                    if exc.expired:
                        self.store.delete_web_push_subscription_by_id(
                            delivery.subscription_id
                        )
                        logger.info(
                            "Removed expired Web Push subscription %d",
                            delivery.subscription_id,
                        )
                        continue
                    attempts = delivery.attempts + 1
                    if (
                        not exc.retryable
                        or attempts >= self.base_settings.telegram_max_attempts
                    ):
                        invalid_subscription = (
                            not exc.retryable
                            and exc.status_code in {None, 400, 401, 403}
                        )
                        if invalid_subscription:
                            dead += self.store.disable_web_push_subscription_by_id(
                                delivery.subscription_id,
                                safe_error,
                                failed_delivery_id=delivery.id,
                            )
                        elif self.store.mark_web_push_dead(delivery.id, safe_error):
                            dead += 1
                        logger.error(
                            "Web Push delivery %d was permanently stopped: %s",
                            delivery.id,
                            safe_error,
                        )
                        continue
                    exponential = self.base_settings.telegram_retry_base_seconds * (
                        2 ** min(delivery.attempts, 6)
                    )
                    retry_after = max(exc.retry_after_seconds or 0, exponential)
                    self.store.mark_web_push_failed(
                        delivery.id,
                        safe_error,
                        retry_after,
                    )
                    logger.error(
                        "Web Push delivery %d failed; retrying in %d seconds: %s",
                        delivery.id,
                        retry_after,
                        safe_error,
                    )
                    break
                else:
                    if self.store.mark_web_push_sent(delivery.id):
                        sent += 1
        return sent, failed, dead

    def heartbeat(self) -> dict[str, Any]:
        targets = self.store.list_targets()
        errors = sum(1 for target in targets if target["last_error"])
        telegram_dead = len(self.store.list_outbox(status="dead", limit=1))
        web_push_dead = len(self.store.list_web_push_deliveries(status="dead", limit=1))
        # Browser Push is a best-effort, per-device channel. Permanent endpoint
        # failures disable that subscription, so retained delivery rows are
        # audit data and must not make the core CGV worker permanently unhealthy.
        dead = telegram_dead
        payload = {
            "timestamp": _now().isoformat(),
            "status": "degraded" if errors or dead else "ok",
            "targets": len(targets),
            "target_errors": errors,
            "dead_letters": dead,
            "web_push_dead_letters": web_push_dead,
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
            try:
                self.deliver_pending()
                self.deliver_pending_web_push()
                for target in self.store.due_targets(limit=10):
                    if stop_event.is_set():
                        break
                    self.process_target(target)
                    self.deliver_pending()
                    self.deliver_pending_web_push()

                if time.monotonic() - last_heartbeat >= 10:
                    self.heartbeat()
                    last_heartbeat = time.monotonic()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                logger.warning("SQLite was busy; worker cycle will retry: %s", exc)
            stop_event.wait(self.console_settings.worker_tick_seconds)


def console_worker_health(settings: ConsoleSettings) -> dict[str, Any]:
    if not settings.database_path.is_file():
        raise RuntimeError("Console database is missing")
    store = ConsoleStore(
        settings.database_path,
        settings.encryption_key,
        initialize=False,
    )
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
