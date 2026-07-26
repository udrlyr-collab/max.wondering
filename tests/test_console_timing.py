from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from moviemax.console_config import ConsoleSettings
from moviemax.console_store import ConsoleStore
from moviemax.console_web import create_app


def test_bootstrap_exposes_current_utc_server_time(tmp_path) -> None:
    settings = ConsoleSettings(
        database_path=tmp_path / "console.sqlite3",
        encryption_key=Fernet.generate_key().decode("ascii"),
        public_origin="https://max.wondering.kr",
        allowed_hosts=("testserver",),
        seed_default_target=False,
    )
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    app = create_app(settings, store=store, catalog_client=object())

    before = datetime.now(UTC)
    with TestClient(app) as client:
        response = client.get("/api/v1/bootstrap")
    after = datetime.now(UTC)

    assert response.status_code == 200
    server_time = datetime.fromisoformat(response.json()["server_time"])
    assert server_time.utcoffset() == timedelta(0)
    assert before <= server_time <= after
