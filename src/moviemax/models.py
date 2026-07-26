from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Screening:
    company_code: str
    site_no: str
    site_name: str
    movie_no: str
    movie_name: str
    screening_date: str
    screen_no: str
    screen_name: str
    sequence: str
    start_time: str
    end_time: str
    format_name: str
    screen_grade_code: str
    total_seats: int
    free_seats: int
    control_yn: str
    booking_url: str

    @property
    def key(self) -> str:
        return (
            f"{self.company_code}|{self.site_no}|{self.movie_no}|"
            f"{self.screening_date}|{self.screen_no}|{self.sequence}"
        )

    @property
    def is_sale_open(self) -> bool:
        return self.control_yn != "Y"

    @property
    def is_bookable(self) -> bool:
        return self.control_yn != "Y" and self.free_seats > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Screening:
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    id: int
    event_key: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    delivered_parts: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> OutboxEvent:
        return cls(
            id=int(row["id"]),
            event_key=str(row["event_key"]),
            kind=str(row["kind"]),
            payload=json.loads(str(row["payload_json"])),
            attempts=int(row["attempts"]),
            delivered_parts=int(row["delivered_parts"]),
        )


@dataclass(frozen=True, slots=True)
class ChangeSummary:
    initialized_before_poll: bool
    screening_count: int
    new_screening_count: int
    booking_opened_count: int
    seat_increase_count: int
