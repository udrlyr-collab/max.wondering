from __future__ import annotations

from dataclasses import replace

from moviemax.models import Screening
from moviemax.state import StateStore


def screening(
    *,
    sequence: str = "1",
    free_seats: int = 0,
    control_yn: str = "N",
) -> Screening:
    return Screening(
        company_code="A420",
        site_no="0013",
        site_name="용산아이파크몰",
        movie_no="30001323",
        movie_name="오디세이",
        screening_date="20260810",
        screen_no="018",
        screen_name="IMAX관",
        sequence=sequence,
        start_time="1000" if sequence == "1" else "1330",
        end_time="1302" if sequence == "1" else "1632",
        format_name="IMAX LASER 2D",
        screen_grade_code="0301",
        total_seats=624,
        free_seats=free_seats,
        control_yn=control_yn,
        booking_url="https://cgv.co.kr/cnm/movieBook/movie?siteNo=0013",
    )


def test_baseline_new_session_and_seat_increase_are_deduplicated(tmp_path) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)

    baseline = store.apply_snapshot([screening()], notify_on_initial_state=False)
    assert baseline.new_screening_count == 0
    assert store.pending_events() == []

    added = store.apply_snapshot(
        [screening(), screening(sequence="2", free_seats=10)],
        notify_on_initial_state=False,
    )
    assert added.new_screening_count == 1
    assert [event.kind for event in store.pending_events()] == ["new_screenings"]

    new_event = store.pending_events()[0]
    store.mark_sent(new_event.id)
    restarted = StateStore(database)
    unchanged = restarted.apply_snapshot(
        [screening(), screening(sequence="2", free_seats=10)],
        notify_on_initial_state=False,
    )
    assert unchanged.new_screening_count == 0
    assert restarted.pending_events() == []

    increased = restarted.apply_snapshot(
        [replace(screening(), free_seats=2), screening(sequence="2", free_seats=10)],
        notify_on_initial_state=False,
    )
    assert increased.seat_increase_count == 1
    pending = restarted.pending_events()
    assert len(pending) == 1
    assert pending[0].kind == "seat_increases"
    assert pending[0].payload["changes"][0]["previous_free_seats"] == 0


def test_initial_notification_can_be_enabled(tmp_path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    result = store.apply_snapshot(
        [screening(free_seats=3)], notify_on_initial_state=True
    )
    assert result.new_screening_count == 1
    assert len(store.pending_events()) == 1


def test_controlled_screening_notifies_when_booking_opens(tmp_path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    prepared = screening(free_seats=20, control_yn="Y")

    baseline = store.apply_snapshot([prepared], notify_on_initial_state=False)
    assert baseline.new_screening_count == 0
    assert baseline.booking_opened_count == 0
    assert store.pending_events() == []

    opened = store.apply_snapshot(
        [replace(prepared, control_yn="N")],
        notify_on_initial_state=False,
    )

    assert opened.booking_opened_count == 1
    assert opened.seat_increase_count == 0
    pending = store.pending_events()
    assert len(pending) == 1
    assert pending[0].kind == "booking_opened"
    assert pending[0].payload["screenings"][0]["control_yn"] == "N"


def test_repeated_identical_seat_increase_creates_distinct_events(tmp_path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.apply_snapshot([screening(free_seats=0)], notify_on_initial_state=False)

    first = store.apply_snapshot(
        [screening(free_seats=1)],
        notify_on_initial_state=False,
    )
    assert first.seat_increase_count == 1
    first_event = store.pending_events()[0]
    store.mark_sent(first_event.id)

    decreased = store.apply_snapshot(
        [screening(free_seats=0)],
        notify_on_initial_state=False,
    )
    assert decreased.seat_increase_count == 0

    second = store.apply_snapshot(
        [screening(free_seats=1)],
        notify_on_initial_state=False,
    )
    assert second.seat_increase_count == 1
    pending = store.pending_events()
    assert len(pending) == 1
    assert pending[0].kind == "seat_increases"
    assert pending[0].event_key != first_event.event_key
    assert pending[0].payload["changes"][0]["previous_free_seats"] == 0
