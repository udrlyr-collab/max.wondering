from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from moviemax.config import ConfigError, Settings, _bool, _int, _secret, load_dotenv


@dataclass(frozen=True, slots=True)
class ConsoleSettings:
    database_path: Path = Path("./data/console.sqlite3")
    encryption_key: str = ""
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    public_origin: str = "http://127.0.0.1:8787"
    allowed_hosts: tuple[str, ...] = (
        "127.0.0.1",
        "localhost",
        "testserver",
        "max.wondering.kr",
    )
    worker_tick_seconds: int = 2
    seed_default_target: bool = False

    @classmethod
    def from_env(cls, *, require_encryption_key: bool = True) -> ConsoleSettings:
        load_dotenv()
        public_origin = os.getenv(
            "CONSOLE_PUBLIC_ORIGIN",
            "http://127.0.0.1:8787",
        ).rstrip("/")
        configured_hosts = tuple(
            value.strip()
            for value in os.getenv(
                "CONSOLE_ALLOWED_HOSTS",
                "127.0.0.1,localhost,testserver,max.wondering.kr",
            ).split(",")
            if value.strip()
        )
        settings = cls(
            database_path=Path(os.getenv("CONSOLE_DB_PATH", "./data/console.sqlite3")),
            encryption_key=_secret("APP_ENCRYPTION_KEY"),
            web_host=os.getenv("CONSOLE_WEB_HOST", "0.0.0.0").strip(),
            web_port=_int("CONSOLE_WEB_PORT", 8000),
            public_origin=public_origin,
            allowed_hosts=configured_hosts,
            worker_tick_seconds=_int("CONSOLE_WORKER_TICK_SECONDS", 2),
            seed_default_target=_bool("SEED_DEFAULT_TARGET", False),
        )
        settings.validate(require_encryption_key=require_encryption_key)
        return settings

    def validate(self, *, require_encryption_key: bool = True) -> None:
        if require_encryption_key and not self.encryption_key:
            raise ConfigError(
                "APP_ENCRYPTION_KEY or APP_ENCRYPTION_KEY_FILE is required"
            )
        if not (1 <= self.web_port <= 65535):
            raise ConfigError("CONSOLE_WEB_PORT must be a valid TCP port")
        if not (1 <= self.worker_tick_seconds <= 30):
            raise ConfigError("CONSOLE_WORKER_TICK_SECONDS must be between 1 and 30")
        parsed = urlparse(self.public_origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(
                "CONSOLE_PUBLIC_ORIGIN must be an absolute HTTP(S) origin"
            )
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ConfigError("CONSOLE_PUBLIC_ORIGIN must not contain a path or query")
        if not self.allowed_hosts:
            raise ConfigError("CONSOLE_ALLOWED_HOSTS cannot be empty")

    @property
    def base_cgv_settings(self) -> Settings:
        return Settings.from_env()
