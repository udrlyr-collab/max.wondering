from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from moviemax.models import OutboxEvent
from moviemax.telegram import (
    TelegramClient,
    TelegramError,
    render_event_messages,
)
from tests.test_state import screening


def test_renders_new_screening_message() -> None:
    item = screening(free_seats=12)
    event = OutboxEvent(
        id=1,
        event_key="new:1",
        kind="new_screenings",
        payload={"screenings": [item.to_dict()]},
        attempts=0,
        delivered_parts=0,
    )
    messages = render_event_messages(event)
    assert len(messages) == 1
    message = messages[0]
    assert "CGV 새 예매 회차" in message
    assert "2026-08-10 (월)" in message
    assert "잔여 12/624석" in message
    assert "해당 회차 예매하기" in message
    assert item.booking_url in message


def test_renders_seat_decrease_with_signed_delta() -> None:
    item = screening(free_seats=3)
    event = OutboxEvent(
        id=5,
        event_key="seat:decrease:1",
        kind="seat_decreases",
        payload={"changes": [{"screening": item.to_dict(), "previous_free_seats": 8}]},
        attempts=0,
        delivered_parts=0,
    )

    messages = render_event_messages(event)

    assert len(messages) == 1
    assert "잔여석 감소 감지" in messages[0]
    assert "잔여 8 → 3석 (-5석)" in messages[0]
    assert "예매 또는 좌석 재고 조정" in messages[0]
    assert item.booking_url in messages[0]


def test_renders_seat_increase_without_claiming_it_is_definitely_a_cancellation() -> (
    None
):
    item = screening(free_seats=2)
    event = OutboxEvent(
        id=2,
        event_key="seat:1",
        kind="seat_increases",
        payload={"changes": [{"screening": item.to_dict(), "previous_free_seats": 0}]},
        attempts=0,
        delivered_parts=0,
    )
    messages = render_event_messages(event)
    assert len(messages) == 1
    message = messages[0]
    assert "잔여 0 → 2석" in message
    assert "취소표 또는 좌석 재고 조정" in message
    assert "해당 회차 예매하기" in message
    assert item.booking_url in message


def test_renders_booking_opened_message() -> None:
    item = screening(free_seats=20)
    event = OutboxEvent(
        id=3,
        event_key="opened:1",
        kind="booking_opened",
        payload={"screenings": [item.to_dict()]},
        attempts=0,
        delivered_parts=0,
    )

    messages = render_event_messages(event)

    assert len(messages) == 1
    assert "CGV 예매 오픈 감지" in messages[0]
    assert "잔여 20/624석" in messages[0]
    assert "해당 회차 예매하기" in messages[0]
    assert item.booking_url in messages[0]


def test_splits_large_event_without_losing_any_screening() -> None:
    markers: list[str] = []
    screenings = []
    for index in range(80):
        marker = f"회차표식-{index:03d}"
        markers.append(marker)
        start_minutes = 6 * 60 + index * 5
        end_minutes = start_minutes + 180
        screenings.append(
            replace(
                screening(sequence=str(index + 1), free_seats=12),
                screen_name=f"{marker}-{'가' * 100}",
                start_time=f"{start_minutes // 60:02d}{start_minutes % 60:02d}",
                end_time=f"{end_minutes // 60:02d}{end_minutes % 60:02d}",
            )
        )
    event = OutboxEvent(
        id=4,
        event_key="new:large",
        kind="new_screenings",
        payload={"screenings": [item.to_dict() for item in screenings]},
        attempts=0,
        delivered_parts=0,
    )

    messages = render_event_messages(event)

    assert len(messages) > 1
    assert all(len(message) <= 3900 for message in messages)
    combined = "\n".join(messages)
    assert all(combined.count(marker) == 1 for marker in markers)


def test_each_screening_uses_its_own_booking_url() -> None:
    first = replace(
        screening(sequence="1", free_seats=12),
        booking_url="https://cgv.co.kr/cnm/movieBook/movie?slot=one",
    )
    second = replace(
        screening(sequence="2", free_seats=8),
        booking_url="https://cgv.co.kr/cnm/movieBook/movie?slot=two",
    )
    event = OutboxEvent(
        id=5,
        event_key="new:urls",
        kind="new_screenings",
        payload={"screenings": [first.to_dict(), second.to_dict()]},
        attempts=0,
        delivered_parts=0,
    )

    combined = "\n".join(render_event_messages(event))

    assert combined.count("해당 회차 예매하기") == 2
    assert first.booking_url in combined
    assert second.booking_url in combined


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> Any:
        return self.payload


def test_bot_info_and_chat_candidates_use_fake_telegram_responses() -> None:
    class FakeSession:
        def get(self, url: str, **_kwargs: object) -> FakeResponse:
            if url.endswith("/getMe"):
                return FakeResponse(
                    200,
                    {
                        "ok": True,
                        "result": {
                            "id": 777,
                            "username": "moviemax_bot",
                            "first_name": "MovieMax",
                        },
                    },
                )
            return FakeResponse(
                200,
                {
                    "ok": True,
                    "result": [
                        {
                            "message": {
                                "chat": {
                                    "id": -100123,
                                    "type": "group",
                                    "title": "MovieMax 알림",
                                }
                            }
                        },
                        {
                            "channel_post": {
                                "chat": {
                                    "id": -100123,
                                    "type": "group",
                                    "title": "중복 항목의 최신 이름",
                                }
                            }
                        },
                    ],
                },
            )

    client = TelegramClient("fake-token", session=FakeSession())

    assert client.bot_info() == {
        "id": "777",
        "username": "moviemax_bot",
        "name": "MovieMax",
    }
    assert client.chat_candidates() == [
        {"id": "-100123", "type": "group", "title": "중복 항목의 최신 이름"}
    ]


def test_rate_limit_error_preserves_retry_metadata() -> None:
    class RateLimitedSession:
        def post(self, _url: str, **_kwargs: object) -> FakeResponse:
            return FakeResponse(
                429,
                {
                    "ok": False,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 17},
                },
            )

    client = TelegramClient(
        "fake-token",
        chat_id="-100123",
        session=RateLimitedSession(),
    )

    with pytest.raises(TelegramError, match="Too Many Requests") as raised:
        client.send_message("test")

    assert raised.value.retryable is True
    assert raised.value.retry_after_seconds == 17


def test_non_object_json_is_reported_as_telegram_error() -> None:
    class InvalidSession:
        def get(self, _url: str, **_kwargs: object) -> FakeResponse:
            return FakeResponse(200, [])

    client = TelegramClient("fake-token", session=InvalidSession())

    with pytest.raises(TelegramError, match="invalid JSON") as raised:
        client.bot_info()

    assert raised.value.retryable is False


def test_transport_error_does_not_expose_bot_token() -> None:
    token = "1234567890:do-not-leak-this-token"

    class ExplodingSession:
        def get(self, _url: str, **_kwargs: object) -> FakeResponse:
            raise RuntimeError(f"failed URL contained {token}")

    client = TelegramClient(token, session=ExplodingSession())

    with pytest.raises(TelegramError) as raised:
        client.bot_info()

    assert raised.value.retryable is True
    assert token not in str(raised.value)
