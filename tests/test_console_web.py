from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from moviemax.console_config import ConsoleSettings
from moviemax.console_store import ConsoleStore
from moviemax.console_web import create_app
from tests.test_console_store import screening


class FakeCatalogClient:
    def __init__(self) -> None:
        self.movie_site_calls: list[str] = []

    def get_regions_and_sites(self) -> list[dict[str, Any]]:
        return [
            {
                "region_code": "01",
                "region_name": "서울",
                "sites": [
                    {
                        "site_no": "0013",
                        "site_name": "용산아이파크몰",
                        "operation_status": "OPEN",
                    }
                ],
            }
        ]

    def get_site_imax_movies(self, site_no: str) -> list[dict[str, Any]]:
        self.movie_site_calls.append(site_no)
        return [
            {
                "movie_no": "30001323",
                "movie_name": "오디세이",
                "formats": ["IMAX LASER 2D"],
                "screening_dates": ["20260810"],
            }
        ]


class FakeTelegramClient:
    created_tokens: ClassVar[list[str]] = []
    sent_messages: ClassVar[list[tuple[str, str, str]]] = []

    def __init__(
        self,
        token: str,
        chat_id: str = "",
        timeout: float = 20.0,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.created_tokens.append(token)

    def bot_info(self) -> dict[str, str]:
        return {"id": "777", "username": "moviemax_test_bot", "name": "MovieMax"}

    def chat_candidates(self) -> list[dict[str, str]]:
        return [{"id": "-100123", "type": "group", "title": "MovieMax 알림"}]

    def send_message(self, message: str) -> None:
        self.sent_messages.append((self.token, self.chat_id, message))


@pytest.fixture
def console_context(tmp_path):
    encryption_key = Fernet.generate_key().decode("ascii")
    settings = ConsoleSettings(
        database_path=tmp_path / "console.sqlite3",
        encryption_key=encryption_key,
        public_origin="https://max.wondering.kr",
        allowed_hosts=("testserver", "max.wondering.kr"),
        worker_tick_seconds=1,
        seed_default_target=False,
    )
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    catalog = FakeCatalogClient()
    app = create_app(settings, store=store, catalog_client=catalog)
    with TestClient(app) as client:
        yield settings, store, catalog, client


def mutation_headers(settings: ConsoleSettings) -> dict[str, str]:
    return {
        "X-MovieMax-CSRF": "1",
        "Origin": settings.public_origin,
    }


def target_payload() -> dict[str, str]:
    return {
        "site_no": "0013",
        "site_name": "용산아이파크몰",
        "movie_no": "30001323",
        "movie_name": "오디세이",
    }


def test_mutations_require_csrf_and_reject_foreign_origin(console_context) -> None:
    settings, _store, _catalog, client = console_context

    untrusted_host = client.get(
        "/api/v1/bootstrap",
        headers={"Host": "evil.example"},
    )
    assert untrusted_host.status_code == 400

    missing_csrf = client.post("/api/v1/targets", json=target_payload())
    assert missing_csrf.status_code == 403
    assert missing_csrf.json() == {"detail": "변경 요청 검증에 실패했습니다"}
    assert missing_csrf.headers["x-content-type-options"] == "nosniff"
    assert missing_csrf.headers["x-frame-options"] == "DENY"

    foreign_origin = client.post(
        "/api/v1/targets",
        json=target_payload(),
        headers={"X-MovieMax-CSRF": "1", "Origin": "https://evil.example"},
    )
    assert foreign_origin.status_code == 403
    assert foreign_origin.json() == {"detail": "허용되지 않은 Origin입니다"}
    assert "frame-ancestors 'none'" in foreign_origin.headers["content-security-policy"]

    accepted = client.post(
        "/api/v1/targets",
        json=target_payload(),
        headers=mutation_headers(settings),
    )
    assert accepted.status_code == 201
    assert accepted.headers["x-content-type-options"] == "nosniff"
    assert accepted.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in accepted.headers["content-security-policy"]


def test_catalog_and_target_create_read_update_refresh(console_context) -> None:
    settings, store, catalog, client = console_context
    headers = mutation_headers(settings)

    sites = client.get("/api/v1/catalog/sites")
    assert sites.status_code == 200
    assert sites.json()["regions"][0]["sites"][0]["site_no"] == "0013"

    movies = client.get("/api/v1/catalog/sites/0013/movies")
    assert movies.status_code == 200
    assert movies.json()["movies"][0]["movie_no"] == "30001323"
    assert catalog.movie_site_calls == ["0013"]
    assert client.get("/api/v1/catalog/sites/bad!id/movies").status_code == 400

    created_response = client.post(
        "/api/v1/targets",
        json=target_payload(),
        headers=headers,
    )
    assert created_response.status_code == 201
    created = created_response.json()["target"]
    assert created["notify_new"] is True
    assert created["auto_track_new"] is False

    duplicate = client.post(
        "/api/v1/targets",
        json=target_payload(),
        headers=headers,
    )
    assert duplicate.status_code == 409

    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    assert [item["id"] for item in bootstrap.json()["targets"]] == [created["id"]]

    updated_response = client.patch(
        f"/api/v1/targets/{created['id']}",
        json={
            "version": created["version"],
            "notify_new": False,
            "auto_track_new": True,
            "poll_interval_seconds": 90,
        },
        headers=headers,
    )
    assert updated_response.status_code == 200
    updated = updated_response.json()["target"]
    assert updated["version"] == created["version"] + 1
    assert updated["notify_new"] is False
    assert updated["auto_track_new"] is True

    stale = client.patch(
        f"/api/v1/targets/{created['id']}",
        json={"version": created["version"], "notify_new": True},
        headers=headers,
    )
    assert stale.status_code == 409

    refresh = client.post(
        f"/api/v1/targets/{created['id']}/refresh",
        headers=headers,
    )
    assert refresh.status_code == 202
    assert refresh.json()["queued"] is True
    assert store.due_targets()[0]["id"] == created["id"]


def test_screening_listing_and_watch_mutation(console_context) -> None:
    settings, store, _catalog, client = console_context
    target = store.ensure_default_target(auto_track_new=False)
    store.apply_snapshot(
        target["id"],
        target["version"],
        [screening(free_seats=4)],
    )
    screening_id = store.list_screenings(target["id"])[0]["id"]

    listed = client.get(f"/api/v1/targets/{target['id']}/screenings")
    assert listed.status_code == 200
    assert listed.json()["screenings"][0]["watched"] is False
    assert listed.json()["screenings"][0]["seat_change_threshold"] == 1
    assert len(listed.json()["screenings"][0]["history"]) == 1

    watched = client.put(
        f"/api/v1/screenings/{screening_id}/watch",
        json={"enabled": True, "seat_change_threshold": 3},
        headers=mutation_headers(settings),
    )
    assert watched.status_code == 200
    assert watched.json()["watch"]["enabled"] is True
    assert watched.json()["watch"]["seat_change_threshold"] == 3
    assert (
        client.get(f"/api/v1/targets/{target['id']}/screenings").json()["screenings"][
            0
        ]["watched"]
        is True
    )

    bulk = client.put(
        f"/api/v1/targets/{target['id']}/watches",
        json={"seat_change_threshold": 5},
        headers=mutation_headers(settings),
    )
    assert bulk.status_code == 200
    assert bulk.json()["bulk_watch_update"] == {
        "target_id": target["id"],
        "seat_change_threshold": 5,
        "updated_count": 1,
    }
    refreshed = client.get(f"/api/v1/targets/{target['id']}/screenings")
    assert refreshed.json()["screenings"][0]["seat_change_threshold"] == 5

    invalid_threshold = client.put(
        f"/api/v1/screenings/{screening_id}/watch",
        json={"enabled": True, "seat_change_threshold": 0},
        headers=mutation_headers(settings),
    )
    assert invalid_threshold.status_code == 422

    overflowing_threshold = client.put(
        f"/api/v1/screenings/{screening_id}/watch",
        json={"enabled": True, "seat_change_threshold": 1 << 53},
        headers=mutation_headers(settings),
    )
    assert overflowing_threshold.status_code == 422

    missing = client.put(
        "/api/v1/screenings/999999/watch",
        json={"enabled": True},
        headers=mutation_headers(settings),
    )
    assert missing.status_code == 404

    missing_target = client.put(
        "/api/v1/targets/999999/watches",
        json={"seat_change_threshold": 2},
        headers=mutation_headers(settings),
    )
    assert missing_target.status_code == 404


def test_telegram_token_never_appears_in_console_responses(
    console_context,
    monkeypatch,
) -> None:
    settings, store, _catalog, client = console_context
    FakeTelegramClient.created_tokens.clear()
    FakeTelegramClient.sent_messages.clear()
    monkeypatch.setattr("moviemax.console_web.TelegramClient", FakeTelegramClient)
    token = "1234567890:super-secret-console-token"
    headers = mutation_headers(settings)

    saved = client.put(
        "/api/v1/telegram",
        json={"bot_token": token, "chat_id": "-100123", "enabled": True},
        headers=headers,
    )
    assert saved.status_code == 200
    assert token not in saved.text
    assert "bot_token" not in saved.json()["telegram"]
    assert saved.json()["telegram"]["bot_username"] == "moviemax_test_bot"

    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    assert token not in bootstrap.text
    assert "bot_token" not in bootstrap.json()["telegram"]
    assert store.get_telegram_config()["token_configured"] is True

    chats = client.post(
        "/api/v1/telegram/chats",
        json={},
        headers=headers,
    )
    assert chats.status_code == 200
    assert chats.json()["chats"] == [
        {"id": "-100123", "type": "group", "title": "MovieMax 알림"}
    ]
    assert token not in chats.text

    sent = client.post("/api/v1/telegram/test", headers=headers)
    assert sent.status_code == 200
    assert sent.json() == {"sent": True}
    assert FakeTelegramClient.sent_messages[0][0:2] == (token, "-100123")


def test_telegram_rejects_empty_chat_before_contacting_bot(
    console_context,
    monkeypatch,
) -> None:
    settings, store, _catalog, client = console_context
    FakeTelegramClient.created_tokens.clear()
    monkeypatch.setattr("moviemax.console_web.TelegramClient", FakeTelegramClient)

    response = client.put(
        "/api/v1/telegram",
        json={
            "bot_token": "1234567890:super-secret-console-token",
            "chat_id": "",
            "enabled": True,
        },
        headers=mutation_headers(settings),
    )

    assert response.status_code == 422
    assert store.get_telegram_config() is None
    assert FakeTelegramClient.created_tokens == []


def test_health_and_bootstrap_report_worker_state_without_network(
    console_context,
) -> None:
    _settings, store, _catalog, client = console_context

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["database"]["journal_mode"] == "wal"

    starting = client.get("/api/v1/bootstrap").json()["status"]
    assert starting == {"worker_ok": False, "status": "starting", "age_seconds": None}

    store.set_metadata(
        "console_worker_heartbeat",
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "status": "ok",
                "target_errors": 0,
                "dead_letters": 0,
            }
        ),
    )
    running = client.get("/api/v1/bootstrap").json()["status"]
    assert running["worker_ok"] is True
    assert running["status"] == "ok"
    assert running["age_seconds"] == 0


def test_console_serves_modernist_assets_without_secrets(console_context) -> None:
    _settings, _store, _catalog, client = console_context

    page = client.get("/")
    stylesheet = client.get("/assets/styles.css")
    script = client.get("/assets/app.js")
    font = client.get("/assets/fonts/WantedSansVariable.woff2")
    font_license = client.get("/assets/fonts/WantedSans-OFL.txt")

    assert page.status_code == stylesheet.status_code == script.status_code == 200
    assert font.status_code == 200
    assert font_license.status_code == 200
    assert font.headers["content-type"].startswith("font/woff2")
    assert len(font.content) == 1_289_292
    assert hashlib.sha256(font.content).hexdigest() == (
        "4259e7e9a172e634c2cb419d793b84148990316341e910443e5d10965b2c8f16"
    )
    assert 'class="topbar"' not in page.text
    assert 'href="/activity"' in page.text
    assert 'id="activityFilterForm"' in page.text
    assert 'id="activityTargetFilter"' in page.text
    assert 'id="activityKindFilter"' in page.text
    assert 'id="activityScreeningFilter"' in page.text
    assert 'id="prevActivityPage"' in page.text
    assert 'id="nextActivityPage"' in page.text
    assert 'id="tokenHelp"' in page.text
    assert 'id="pollSettingsForm"' in page.text
    assert 'id="bulkThresholdForm"' in page.text
    assert 'id="bulkThreshold"' in page.text
    assert 'max="9007199254740991"' in page.text
    assert 'id="pollJitter" type="number" min="0" max="300"' in page.text
    assert re.search(r'id="chatId"[^>]*\brequired\b', page.text)
    assert 'id="telegramFeedback"' in page.text
    assert 'content="light"' in page.text
    assert "@font-face" in stylesheet.text
    assert (
        'url("/assets/fonts/WantedSansVariable.woff2") format("woff2-variations")'
        in stylesheet.text
    )
    assert 'font-family: "Wanted Sans"' in stylesheet.text
    assert "@keyframes seat-rise" in stylesheet.text
    assert "@keyframes seat-drop" not in stylesheet.text
    assert "@media (prefers-reduced-motion: reduce)" in stylesheet.text
    assert "liquid" not in stylesheet.text.lower()
    assert "gradient" not in stylesheet.text.lower()
    assert "apiErrorMessage" in script.text
    assert "latestHistorySummary" in script.text
    assert (
        'window.location.pathname.replace(/\\/+$/, "") === "/activity"' in script.text
    )
    assert "api(`/api/v1/activity?${params.toString()}`)" in script.text
    assert 'notifications_only: "true"' in script.text
    assert "state.recentAlerts" in script.text
    assert "state.screeningThresholdDrafts" in script.text
    assert "state.dashboardRequests" in script.text
    assert "recentAlertsRenderSignature" in script.text
    assert "seat_change_threshold" in script.text
    assert "activityChangeText" in script.text
    assert "activityBookingUrl" in script.text
    assert ">예매하기" in script.text
    assert "setInterval(updateLiveTimes, 1000)" in script.text
    combined_assets = page.text + stylesheet.text + script.text
    assert re.search(r"\b\d{8,12}:AA[A-Za-z0-9_-]{20,}\b", combined_assets) is None
