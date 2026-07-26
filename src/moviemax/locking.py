from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self


class AlreadyRunningError(RuntimeError):
    """Raised when another monitor process owns the state lock."""


class ProcessLock:
    def __init__(self, path: Path | str, *, blocking: bool = False) -> None:
        self.path = Path(path)
        self.blocking = blocking
        self._handle: BinaryIO | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX
                if not self.blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            handle.close()
            raise AlreadyRunningError(
                "Another MovieMax monitor is already using this state directory"
            ) from exc
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class BlockingFileLock(ProcessLock):
    """Cross-process exclusive lock that waits for the current holder."""

    def __init__(self, path: Path | str) -> None:
        super().__init__(path, blocking=True)
