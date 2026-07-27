from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from moviemax.polling import MAX_POLL_JITTER_SECONDS, MIN_POLL_INTERVAL_SECONDS


class ConfigError(ValueError):
    """Raised when runtime configuration is missing or invalid."""


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(path: Path | str = ".env") -> None:
    """Load a small, predictable KEY=VALUE file without overriding the process env."""
    env_path = Path(path)
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_NAME.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _secret(name: str) -> str:
    direct = os.getenv(name, "").strip()
    if direct:
        return direct
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if not file_name:
        return ""
    try:
        return Path(file_name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"Cannot read {name}_FILE: {file_name}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    cgv_base_url: str = "https://cgv.co.kr"
    company_code: str = "A420"
    site_no: str = "0013"
    site_name: str = "용산아이파크몰"
    movie_no: str = "30001323"
    movie_name: str = "오디세이"
    format_code: str = ""
    format_keyword: str = "IMAX"
    screen_grade_code: str = "0301"
    poll_interval_seconds: int = 60
    poll_jitter_seconds: int = 5
    request_timeout_seconds: float = 20.0
    request_gap_seconds: float = 0.25
    backoff_max_seconds: int = 900
    telegram_max_attempts: int = 10
    telegram_retry_base_seconds: int = 60
    notification_health_failure_threshold: int = 3
    notify_on_initial_state: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    state_db_path: Path = Path("./data/moviemax.sqlite3")
    heartbeat_path: Path = Path("./data/heartbeat.json")
    lock_file_path: Path | None = None
    log_level: str = "INFO"
    cgv_impersonate: str = "chrome"

    @classmethod
    def from_env(
        cls,
        *,
        require_telegram_token: bool = False,
        require_telegram_chat: bool = False,
    ) -> Settings:
        load_dotenv()
        settings = cls(
            cgv_base_url=os.getenv("CGV_BASE_URL", "https://cgv.co.kr").rstrip("/"),
            company_code=os.getenv("CGV_COMPANY_CODE", "A420").strip(),
            site_no=os.getenv("CGV_SITE_NO", "0013").strip(),
            site_name=os.getenv("CGV_SITE_NAME", "용산아이파크몰").strip(),
            movie_no=os.getenv("CGV_MOVIE_NO", "30001323").strip(),
            movie_name=os.getenv("CGV_MOVIE_NAME", "오디세이").strip(),
            format_code=os.getenv("CGV_FORMAT_CODE", "").strip(),
            format_keyword=os.getenv("CGV_FORMAT_KEYWORD", "IMAX").strip(),
            screen_grade_code=os.getenv("CGV_SCREEN_GRADE_CODE", "0301").strip(),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 60),
            poll_jitter_seconds=_int("POLL_JITTER_SECONDS", 5),
            request_timeout_seconds=_float("REQUEST_TIMEOUT_SECONDS", 20.0),
            request_gap_seconds=_float("REQUEST_GAP_SECONDS", 0.25),
            backoff_max_seconds=_int("BACKOFF_MAX_SECONDS", 900),
            telegram_max_attempts=_int("TELEGRAM_MAX_ATTEMPTS", 10),
            telegram_retry_base_seconds=_int("TELEGRAM_RETRY_BASE_SECONDS", 60),
            notification_health_failure_threshold=_int(
                "NOTIFICATION_HEALTH_FAILURE_THRESHOLD", 3
            ),
            notify_on_initial_state=_bool("NOTIFY_ON_INITIAL_STATE", False),
            telegram_bot_token=_secret("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_secret("TELEGRAM_CHAT_ID"),
            state_db_path=Path(os.getenv("STATE_DB_PATH", "./data/moviemax.sqlite3")),
            heartbeat_path=Path(os.getenv("HEARTBEAT_PATH", "./data/heartbeat.json")),
            lock_file_path=(
                Path(os.environ["LOCK_FILE_PATH"])
                if os.getenv("LOCK_FILE_PATH", "").strip()
                else None
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            cgv_impersonate=os.getenv("CGV_IMPERSONATE", "chrome").strip(),
        )
        settings.validate(
            require_telegram_token=require_telegram_token,
            require_telegram_chat=require_telegram_chat,
        )
        return settings

    def validate(
        self,
        *,
        require_telegram_token: bool = False,
        require_telegram_chat: bool = False,
    ) -> None:
        if not self.site_no or not self.site_name:
            raise ConfigError("CGV site number and name are required")
        if not self.movie_no and not self.movie_name:
            raise ConfigError("CGV_MOVIE_NO or CGV_MOVIE_NAME is required")
        if (
            not self.format_code
            and not self.format_keyword
            and not self.screen_grade_code
        ):
            raise ConfigError(
                "A format code, keyword, or screen grade code is required"
            )
        if self.poll_interval_seconds < MIN_POLL_INTERVAL_SECONDS:
            raise ConfigError(
                f"POLL_INTERVAL_SECONDS must be at least {MIN_POLL_INTERVAL_SECONDS}"
            )
        if not 0 <= self.poll_jitter_seconds <= MAX_POLL_JITTER_SECONDS:
            raise ConfigError(
                f"POLL_JITTER_SECONDS must be between 0 and {MAX_POLL_JITTER_SECONDS}"
            )
        if self.request_timeout_seconds <= 0 or self.request_gap_seconds < 0:
            raise ConfigError("Request timeout/gap values are invalid")
        if self.backoff_max_seconds < self.poll_interval_seconds:
            raise ConfigError("BACKOFF_MAX_SECONDS must be at least the poll interval")
        if self.telegram_max_attempts < 1 or self.telegram_retry_base_seconds < 1:
            raise ConfigError("Telegram retry settings must be positive")
        if self.notification_health_failure_threshold < 1:
            raise ConfigError("Notification health threshold must be positive")
        if require_telegram_token and not self.telegram_bot_token:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN_FILE is required"
            )
        if require_telegram_chat and not self.telegram_chat_id:
            raise ConfigError("TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID_FILE is required")

    @property
    def health_max_age_seconds(self) -> int:
        return max(300, self.poll_interval_seconds * 5)

    @property
    def process_lock_path(self) -> Path:
        return self.lock_file_path or self.state_db_path.with_suffix(".lock")
