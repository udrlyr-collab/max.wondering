from __future__ import annotations

import pytest

from moviemax.locking import AlreadyRunningError, ProcessLock


def test_process_lock_rejects_second_owner_and_can_be_reacquired(tmp_path) -> None:
    lock_path = tmp_path / "monitor.lock"

    with (
        ProcessLock(lock_path),
        pytest.raises(AlreadyRunningError, match="already using"),
        ProcessLock(lock_path),
    ):
        pytest.fail("the second process lock must not be acquired")

    with ProcessLock(lock_path):
        assert lock_path.is_file()
