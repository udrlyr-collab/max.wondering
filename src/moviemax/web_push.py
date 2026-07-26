from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlparse

import requests
from pywebpush import WebPushException, webpush

from moviemax.models import OutboxEvent


@dataclass(frozen=True, slots=True)
class WebPushError(RuntimeError):
    message: str
    retryable: bool = False
    expired: bool = False
    retry_after_seconds: int | None = None
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0, int((parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds()))


def _display_date(value: Any) -> str:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{int(text[4:6])}월 {int(text[6:8])}일"
    return text or "날짜 미확인"


def _display_time(value: Any) -> str:
    text = str(value or "")
    if len(text) == 4 and text.isdigit():
        return f"{text[:2]}:{text[2:]}"
    return text or "시간 미확인"


def render_web_push_payload(
    event: OutboxEvent,
    *,
    fallback_url: str,
) -> dict[str, Any]:
    if event.kind != "seat_increases":
        raise ValueError("Web Push only supports seat-increase events")
    changes = event.payload.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("seat-increase event has no changes")
    change = changes[0]
    if not isinstance(change, Mapping):
        raise TypeError("seat-increase event change is invalid")
    screening = change.get("screening")
    previous = change.get("previous_screening")
    if not isinstance(screening, Mapping):
        raise TypeError("seat-increase event has no screening")
    previous_free = change.get("previous_free_seats")
    if previous_free is None and isinstance(previous, Mapping):
        previous_free = previous.get("free_seats")
    current_free = screening.get("free_seats")
    try:
        before = int(previous_free)
        after = int(current_free)
    except (TypeError, ValueError) as exc:
        raise ValueError("seat-increase event has invalid seat counts") from exc
    delta = after - before
    if delta <= 0:
        raise ValueError("seat-increase event does not contain an increase")

    movie_name = str(screening.get("movie_name") or "영화")
    site_name = str(screening.get("site_name") or "CGV")
    format_name = str(screening.get("format_name") or "상영 포맷")
    screen_name = str(screening.get("screen_name") or "상영관 미확인")
    session = (
        f"{_display_date(screening.get('screening_date'))} "
        f"{_display_time(screening.get('start_time'))}"
    )
    booking_url = str(screening.get("booking_url") or "")
    parsed_booking = urlparse(booking_url)
    booking_host = (parsed_booking.hostname or "").rstrip(".").lower()
    if (
        parsed_booking.scheme == "https"
        and parsed_booking.username is None
        and parsed_booking.password is None
        and (booking_host == "cgv.co.kr" or booking_host.endswith(".cgv.co.kr"))
    ):
        click_url = (
            f"{fallback_url.rstrip('/')}/booking?url={quote(booking_url, safe='')}"
        )
    else:
        click_url = fallback_url
    return {
        "title": f"잔여석 +{delta} · {movie_name}",
        "body": (
            f"{site_name} · {format_name} · {session}\n"
            f"{screen_name} · {before} → {after}석"
        ),
        "tag": f"moviemax-{event.event_key}",
        "url": click_url,
    }


class WebPushClient:
    def __init__(
        self,
        private_key: str,
        subject: str,
        timeout: float,
        *,
        ttl_seconds: int = 300,
    ) -> None:
        self.private_key = private_key
        self.subject = subject
        self.timeout = timeout
        self.ttl_seconds = ttl_seconds

    def send(
        self,
        subscription_info: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        try:
            webpush(
                subscription_info=dict(subscription_info),
                data=json.dumps(
                    dict(payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                vapid_private_key=self.private_key,
                vapid_claims={"sub": self.subject},
                timeout=self.timeout,
                ttl=self.ttl_seconds,
            )
        except requests.RequestException as exc:
            raise WebPushError(
                f"Web Push connection failed ({type(exc).__name__})",
                retryable=True,
            ) from exc
        except WebPushException as exc:
            response = exc.response
            if response is None:
                raise WebPushError(
                    "Web Push payload or VAPID key was rejected"
                ) from exc
            status_code = int(response.status_code)
            retryable = status_code in {408, 425, 429} or status_code >= 500
            expired = status_code in {404, 410}
            raise WebPushError(
                f"Web Push service returned HTTP {status_code}",
                retryable=retryable,
                expired=expired,
                retry_after_seconds=_retry_after_seconds(
                    response.headers.get("Retry-After")
                ),
                status_code=status_code,
            ) from exc
        except Exception as exc:
            raise WebPushError(
                f"Web Push request could not be created ({type(exc).__name__})"
            ) from exc
