from __future__ import annotations

import html
from datetime import date
from typing import Any

from curl_cffi import requests

from moviemax.models import OutboxEvent, Screening


class TelegramError(RuntimeError):
    """Raised when Telegram rejects or cannot receive a message."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


_WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


def _date(value: str) -> str:
    parsed = date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")
    return f"{parsed:%Y-%m-%d} ({_WEEKDAYS[parsed.weekday()]})"


def _time(value: str) -> str:
    if len(value) == 4 and value.isdigit():
        return f"{value[:2]}:{value[2:]}"
    return value


def _availability(screening: Screening) -> str:
    if not screening.is_sale_open:
        return "예매 준비중"
    if screening.free_seats <= 0:
        return f"매진 / 총 {screening.total_seats}석"
    return f"잔여 {screening.free_seats}/{screening.total_seats}석"


def _safe(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _assemble_messages(
    header: list[str],
    entries: list[str],
    footer: list[str],
    limit: int = 3900,
) -> list[str]:
    messages: list[str] = []
    current: list[str] = []
    for entry in entries:
        candidate = "\n".join(header + current + [entry] + footer)
        if len(candidate) <= limit:
            current.append(entry)
            continue
        if not current:
            raise TelegramError("One notification entry exceeds Telegram's limit")
        messages.append("\n".join(header + current + footer))
        current = [entry]
        if len("\n".join(header + current + footer)) > limit:
            raise TelegramError("One notification entry exceeds Telegram's limit")
    if current:
        messages.append("\n".join(header + current + footer))
    if not messages:
        raise TelegramError("Notification event has no entries")
    return messages


def _screening_event_messages(event: OutboxEvent, title: str) -> list[str]:
    screenings = [
        Screening.from_dict(item) for item in event.payload.get("screenings", [])
    ]
    screenings.sort(
        key=lambda item: (item.screening_date, item.start_time, item.screen_no)
    )
    if not screenings:
        raise TelegramError("Screening event has no screenings")
    first = screenings[0]
    header = [
        title,
        f"<b>{_safe(first.movie_name)}</b> · CGV {_safe(first.site_name)}",
        "",
    ]
    entries = [
        (
            f"• <b>{_safe(_date(item.screening_date))}</b> "
            f"{_safe(_time(item.start_time))}–{_safe(_time(item.end_time))}\n"
            f"  {_safe(item.screen_name)} · {_safe(item.format_name)} · "
            f"{_safe(_availability(item))}\n"
            f'  <a href="{_safe(item.booking_url)}">해당 회차 예매하기</a>'
        )
        for item in screenings
    ]
    return _assemble_messages(header, entries, [])


def render_event_messages(event: OutboxEvent) -> list[str]:
    if event.kind == "new_screenings":
        return _screening_event_messages(
            event,
            "🎬 <b>CGV IMAX 새 예매 회차 감지</b>",
        )
    if event.kind == "booking_opened":
        return _screening_event_messages(
            event,
            "🚨 <b>CGV IMAX 예매 오픈 감지</b>",
        )
    if event.kind in {"seat_increases", "seat_decreases"}:
        changes = event.payload.get("changes", [])
        if not changes:
            raise TelegramError("Seat-change event has no changes")
        parsed = [
            (Screening.from_dict(item["screening"]), int(item["previous_free_seats"]))
            for item in changes
        ]
        parsed.sort(key=lambda item: (item[0].screening_date, item[0].start_time))
        first = parsed[0][0]
        increasing = event.kind == "seat_increases"
        header = [
            (
                "🎟️ <b>CGV IMAX 잔여석 증가 감지</b>"
                if increasing
                else "🔻 <b>CGV IMAX 잔여석 감소 감지</b>"
            ),
            f"<b>{_safe(first.movie_name)}</b> · CGV {_safe(first.site_name)}",
            (
                "취소표 또는 좌석 재고 조정일 수 있습니다."
                if increasing
                else "예매 또는 좌석 재고 조정일 수 있습니다."
            ),
            "",
        ]
        entries = [
            (
                f"• <b>{_safe(_date(item.screening_date))}</b> "
                f"{_safe(_time(item.start_time))}–{_safe(_time(item.end_time))}\n"
                f"  {_safe(item.screen_name)} · 잔여 {_safe(old)} → "
                f"{_safe(item.free_seats)}석 "
                f"({_safe(f'{item.free_seats - old:+d}')}석) / "
                f"총 {_safe(item.total_seats)}석\n"
                f'  <a href="{_safe(item.booking_url)}">해당 회차 예매하기</a>'
            )
            for item, old in parsed
        ]
        return _assemble_messages(header, entries, [])
    raise TelegramError(f"Unsupported outbox event kind: {event.kind}")


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str = "",
        timeout: float = 20.0,
        session: Any | None = None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.session = session or requests.Session()

    def _call(
        self,
        method: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            if payload is None:
                response = self.session.get(url, timeout=self.timeout)
            else:
                response = self.session.post(url, json=payload, timeout=self.timeout)
        except Exception as exc:
            raise TelegramError(
                f"Telegram connection failed ({type(exc).__name__})",
                retryable=True,
            ) from exc

        try:
            body = response.json()
        except Exception as exc:
            raise TelegramError(
                f"Telegram returned HTTP {response.status_code} with invalid JSON",
                retryable=int(response.status_code) >= 500,
            ) from exc
        if not isinstance(body, dict):
            raise TelegramError(
                f"Telegram returned HTTP {response.status_code} with invalid JSON",
                retryable=int(response.status_code) >= 500,
            )

        if int(response.status_code) != 200 or not body.get("ok"):
            status = int(response.status_code)
            description = str(body.get("description") or f"Telegram HTTP {status}")
            parameters = body.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            try:
                retry_after_seconds = (
                    int(retry_after) if retry_after is not None else None
                )
            except (TypeError, ValueError):
                retry_after_seconds = None
            raise TelegramError(
                description,
                retryable=status in {408, 429} or status >= 500,
                retry_after_seconds=retry_after_seconds,
            )
        return dict(body)

    def send_message(self, text: str) -> None:
        if not self.chat_id:
            raise TelegramError("Telegram chat ID is not configured")
        self._call(
            "sendMessage",
            payload={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def bot_info(self) -> dict[str, Any]:
        body = self._call("getMe")
        result = body.get("result")
        if not isinstance(result, dict) or "id" not in result:
            raise TelegramError("Telegram returned invalid bot information")
        return {
            "id": str(result["id"]),
            "username": str(result.get("username") or ""),
            "name": str(result.get("first_name") or ""),
        }

    def chat_candidates(self) -> list[dict[str, Any]]:
        body = self._call("getUpdates")
        candidates: dict[str, dict[str, Any]] = {}
        for update in body.get("result", []):
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if "id" not in chat:
                continue
            chat_id = str(chat["id"])
            candidates[chat_id] = {
                "id": chat_id,
                "type": chat.get("type", ""),
                "title": chat.get("title")
                or chat.get("username")
                or chat.get("first_name")
                or "",
            }
        return list(candidates.values())
