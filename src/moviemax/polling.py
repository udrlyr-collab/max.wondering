from __future__ import annotations

import random

MAX_POLL_JITTER_SECONDS = 300


def normalize_poll_jitter_seconds(value: int) -> int:
    jitter = int(value)
    if not 0 <= jitter <= MAX_POLL_JITTER_SECONDS:
        raise ValueError(
            f"poll_jitter_seconds must be between 0 and {MAX_POLL_JITTER_SECONDS}"
        )
    return jitter


def jittered_delay_seconds(
    base_delay_seconds: float,
    jitter_seconds: int,
) -> float:
    base_delay = float(base_delay_seconds)
    if base_delay < 0:
        raise ValueError("base_delay_seconds cannot be negative")
    jitter = normalize_poll_jitter_seconds(jitter_seconds)
    return base_delay + random.uniform(0, jitter)
