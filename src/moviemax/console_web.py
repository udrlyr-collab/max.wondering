from __future__ import annotations

import json
import mimetypes
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from moviemax.cgv import CgvClient, CgvError
from moviemax.console_config import ConsoleSettings
from moviemax.console_store import (
    MAX_SEAT_CHANGE_THRESHOLD,
    ConsoleStore,
    StaleVersionError,
)
from moviemax.polling import MAX_POLL_JITTER_SECONDS
from moviemax.telegram import TelegramClient, TelegramError

_ASSETS = Path(__file__).parent / "web_assets"
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("font/woff2", ".woff2")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_MAX_BODY_BYTES = 32 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{label} 형식이 올바르지 않습니다")
    return normalized


def _display_name(value: str, label: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 100
        or any(ord(char) < 32 for char in normalized)
    ):
        raise ValueError(f"{label} 형식이 올바르지 않습니다")
    return normalized


class TargetCreate(StrictModel):
    site_no: str
    site_name: str
    movie_no: str
    movie_name: str
    poll_interval_seconds: int | None = Field(default=None, ge=30, le=3600)
    poll_jitter_seconds: int | None = Field(
        default=None,
        ge=0,
        le=MAX_POLL_JITTER_SECONDS,
    )

    @field_validator("site_no")
    @classmethod
    def validate_site_no(cls, value: str) -> str:
        return _identifier(value, "극장 번호")

    @field_validator("movie_no")
    @classmethod
    def validate_movie_no(cls, value: str) -> str:
        return _identifier(value, "영화 번호")

    @field_validator("site_name")
    @classmethod
    def validate_site_name(cls, value: str) -> str:
        return _display_name(value, "극장 이름")

    @field_validator("movie_name")
    @classmethod
    def validate_movie_name(cls, value: str) -> str:
        return _display_name(value, "영화 이름")


class TargetUpdate(StrictModel):
    version: int = Field(ge=1)
    enabled: bool | None = None
    notify_new: bool | None = None
    auto_track_new: bool | None = None
    poll_interval_seconds: int | None = Field(default=None, ge=30, le=3600)
    poll_jitter_seconds: int | None = Field(
        default=None,
        ge=0,
        le=MAX_POLL_JITTER_SECONDS,
    )


class WatchUpdate(StrictModel):
    enabled: bool
    seat_change_threshold: int | None = Field(
        default=None,
        ge=1,
        le=MAX_SEAT_CHANGE_THRESHOLD,
    )


class BulkWatchThresholdUpdate(StrictModel):
    seat_change_threshold: int = Field(ge=1, le=MAX_SEAT_CHANGE_THRESHOLD)


class TelegramUpdate(StrictModel):
    bot_token: str | None = Field(default=None, min_length=20, max_length=256)
    chat_id: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    version: int | None = Field(default=None, ge=1)

    @field_validator("bot_token", "chat_id")
    @classmethod
    def strip_value(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class TelegramCandidateRequest(StrictModel):
    bot_token: str | None = Field(default=None, min_length=20, max_length=256)

    @field_validator("bot_token")
    @classmethod
    def strip_token(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


def _masked_chat_id(chat_id: str) -> str:
    if len(chat_id) <= 6:
        return "•" * len(chat_id)
    return f"{chat_id[:3]}{'•' * max(3, len(chat_id) - 6)}{chat_id[-3:]}"


def _telegram_public(store: ConsoleStore) -> dict[str, Any]:
    config = store.get_telegram_config()
    if config is None:
        return {
            "configured": False,
            "enabled": False,
            "chat_id": "",
            "chat_id_masked": "",
            "bot_username": "",
            "version": 0,
        }
    chat_id = str(config["chat_id"])
    return {
        "configured": True,
        "enabled": bool(config["enabled"]),
        "chat_id": chat_id,
        "chat_id_masked": _masked_chat_id(chat_id),
        "bot_username": store.get_metadata("telegram_bot_username") or "",
        "version": int(config["version"]),
        "updated_at": config["updated_at"],
    }


def _worker_status(store: ConsoleStore) -> dict[str, Any]:
    raw = store.get_metadata("console_worker_heartbeat")
    if raw is None:
        return {"worker_ok": False, "status": "starting", "age_seconds": None}
    try:
        heartbeat = json.loads(raw)
        timestamp = datetime.fromisoformat(str(heartbeat["timestamp"]))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - timestamp.astimezone(UTC)).total_seconds()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {"worker_ok": False, "status": "invalid", "age_seconds": None}
    repeated_failures = any(
        target["enabled"] and int(target["consecutive_failures"]) >= 3
        for target in store.list_targets()
    )
    return {
        "worker_ok": age <= 60 and not repeated_failures,
        "status": heartbeat.get("status", "unknown"),
        "age_seconds": max(0, int(age)),
        "target_errors": heartbeat.get("target_errors", 0),
        "dead_letters": heartbeat.get("dead_letters", 0),
    }


def _get_configured_telegram(store: ConsoleStore) -> dict[str, Any]:
    config = store.get_telegram_config(include_token=True)
    if config is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Telegram 봇을 먼저 설정하세요")
    return config


def create_app(
    settings: ConsoleSettings | None = None,
    *,
    store: ConsoleStore | None = None,
    catalog_client: CgvClient | None = None,
) -> FastAPI:
    configured = settings or ConsoleSettings.from_env()
    base_settings = configured.base_cgv_settings
    database = store or ConsoleStore(
        configured.database_path, configured.encryption_key
    )
    if configured.seed_default_target:
        database.ensure_default_target(
            company_code=base_settings.company_code,
            site_no=base_settings.site_no,
            site_name=base_settings.site_name,
            movie_no=base_settings.movie_no,
            movie_name=base_settings.movie_name,
            format_keyword=base_settings.format_keyword,
            screen_grade_code=base_settings.screen_grade_code,
            poll_interval_seconds=base_settings.poll_interval_seconds,
            poll_jitter_seconds=base_settings.poll_jitter_seconds,
        )
    cgv = catalog_client or CgvClient(base_settings)

    app = FastAPI(
        title="MovieMax Console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.console_settings = configured
    app.state.store = database
    app.state.catalog_client = cgv
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(configured.allowed_hosts)
    )

    @app.middleware("http")
    async def secure_requests(request: Request, call_next: Any) -> Any:
        response: Any | None = None
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > _MAX_BODY_BYTES:
                    response = JSONResponse(
                        {"detail": "요청 본문이 너무 큽니다"},
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    )
            except ValueError:
                response = JSONResponse(
                    {"detail": "Content-Length가 올바르지 않습니다"},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
        if (
            response is None
            and request.url.path.startswith("/api/")
            and request.method in _MUTATION_METHODS
        ):
            if request.headers.get("X-MovieMax-CSRF") != "1":
                response = JSONResponse(
                    {"detail": "변경 요청 검증에 실패했습니다"},
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            origin = request.headers.get("origin")
            if (
                response is None
                and origin
                and origin.rstrip("/") != configured.public_origin
            ):
                response = JSONResponse(
                    {"detail": "허용되지 않은 Origin입니다"},
                    status_code=status.HTTP_403_FORBIDDEN,
                )

        if response is None:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(StaleVersionError)
    async def stale_version_handler(_request: Request, _exc: StaleVersionError) -> Any:
        return JSONResponse(
            {
                "detail": "설정이 다른 화면에서 변경되었습니다. 새로고침 후 다시 시도하세요."
            },
            status_code=status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(CgvError)
    async def cgv_error_handler(_request: Request, exc: CgvError) -> Any:
        return JSONResponse(
            {"detail": f"CGV 조회 실패: {exc}"},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    @app.exception_handler(TelegramError)
    async def telegram_error_handler(_request: Request, exc: TelegramError) -> Any:
        return JSONResponse(
            {"detail": f"Telegram 요청 실패: {exc}"},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"status": "ok", "database": database.health_check()}

    @app.get("/api/v1/bootstrap")
    def bootstrap() -> dict[str, Any]:
        return {
            "server_time": datetime.now(UTC).isoformat(),
            "targets": database.list_targets(),
            "activity": database.list_activity_page(limit=50)["items"],
            "telegram": _telegram_public(database),
            "status": _worker_status(database),
        }

    @app.get("/api/v1/activity")
    def activity(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: int | None = Query(default=None, ge=1),
        target_id: int | None = Query(default=None, ge=1),
        screening_id: int | None = Query(default=None, ge=1),
        notifications_only: bool = Query(default=False),
        kind: Literal[
            "new_screenings",
            "booking_opened",
            "booking_closed",
            "seat_increases",
            "seat_decreases",
            "total_seats_changed",
            "screening_updated",
        ]
        | None = None,
    ) -> dict[str, Any]:
        return database.list_activity_page(
            limit=limit,
            cursor=cursor,
            target_id=target_id,
            screening_id=screening_id,
            kind=kind,
            notifications_only=notifications_only,
        )

    @app.get("/api/v1/catalog/sites")
    def catalog_sites() -> dict[str, Any]:
        regions = cgv.get_regions_and_sites()
        return {
            "regions": [
                {
                    "code": region["region_code"],
                    "name": region["region_name"],
                    "sites": [
                        {
                            "site_no": site["site_no"],
                            "site_name": site["site_name"],
                            "status": site["operation_status"],
                        }
                        for site in region["sites"]
                    ],
                }
                for region in regions
            ]
        }

    @app.get("/api/v1/catalog/sites/{site_no}/movies")
    def catalog_movies(site_no: str) -> dict[str, Any]:
        try:
            validated = _identifier(site_no, "극장 번호")
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        movies = cgv.get_site_imax_movies(validated)
        return {
            "movies": [
                {
                    "movie_no": movie["movie_no"],
                    "movie_name": movie["movie_name"],
                    "formats": movie["formats"],
                    "dates": movie["screening_dates"],
                }
                for movie in movies
            ]
        }

    @app.post("/api/v1/targets", status_code=status.HTTP_201_CREATED)
    def create_target(payload: TargetCreate) -> dict[str, Any]:
        try:
            target = database.create_target(
                company_code="A420",
                site_no=payload.site_no,
                site_name=payload.site_name,
                movie_no=payload.movie_no,
                movie_name=payload.movie_name,
                format_keyword="IMAX",
                screen_grade_code="0301",
                enabled=True,
                notify_new=True,
                auto_track_new=False,
                poll_interval_seconds=(
                    payload.poll_interval_seconds
                    if payload.poll_interval_seconds is not None
                    else base_settings.poll_interval_seconds
                ),
                poll_jitter_seconds=(
                    payload.poll_jitter_seconds
                    if payload.poll_jitter_seconds is not None
                    else base_settings.poll_jitter_seconds
                ),
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {"target": target}

    @app.patch("/api/v1/targets/{target_id}")
    def update_target(target_id: int, payload: TargetUpdate) -> dict[str, Any]:
        changes = payload.model_dump(exclude={"version"}, exclude_none=True)
        try:
            target = database.update_target(
                target_id,
                changes,
                expected_version=payload.version,
            )
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "감시 대상을 찾을 수 없습니다"
            ) from exc
        return {"target": target}

    @app.post(
        "/api/v1/targets/{target_id}/refresh", status_code=status.HTTP_202_ACCEPTED
    )
    def refresh_target(target_id: int) -> dict[str, Any]:
        try:
            target = database.request_refresh(target_id)
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "감시 대상을 찾을 수 없습니다"
            ) from exc
        return {"target": target, "queued": True}

    @app.get("/api/v1/targets/{target_id}/screenings")
    def target_screenings(target_id: int) -> dict[str, Any]:
        if database.get_target(target_id) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "감시 대상을 찾을 수 없습니다"
            )
        return {
            "screenings": database.list_screenings(
                target_id,
                include_history=True,
            )
        }

    @app.put("/api/v1/screenings/{screening_id}/watch")
    def set_screening_watch(
        screening_id: int,
        payload: WatchUpdate,
    ) -> dict[str, Any]:
        try:
            watch = database.set_watch(
                screening_id,
                payload.enabled,
                payload.seat_change_threshold,
            )
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "상영 회차를 찾을 수 없습니다"
            ) from exc
        return {"watch": watch}

    @app.put("/api/v1/targets/{target_id}/watches")
    def set_target_watch_thresholds(
        target_id: int,
        payload: BulkWatchThresholdUpdate,
    ) -> dict[str, Any]:
        try:
            result = database.set_watched_thresholds(
                target_id,
                payload.seat_change_threshold,
            )
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "감시 대상을 찾을 수 없습니다",
            ) from exc
        return {"bulk_watch_update": result}

    @app.put("/api/v1/telegram")
    def save_telegram(payload: TelegramUpdate) -> dict[str, Any]:
        username: str | None = None
        if payload.bot_token:
            info = TelegramClient(
                payload.bot_token,
                timeout=base_settings.request_timeout_seconds,
            ).bot_info()
            username = info["username"]
        try:
            database.save_telegram_config(
                bot_token=payload.bot_token,
                chat_id=payload.chat_id,
                enabled=payload.enabled,
                expected_version=payload.version,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if username is not None:
            database.set_metadata("telegram_bot_username", username)
        return {"telegram": _telegram_public(database)}

    @app.post("/api/v1/telegram/chats")
    def telegram_chats(payload: TelegramCandidateRequest) -> dict[str, Any]:
        token = payload.bot_token
        if not token:
            token = str(_get_configured_telegram(database)["bot_token"])
        client = TelegramClient(
            token,
            timeout=base_settings.request_timeout_seconds,
        )
        return {"chats": client.chat_candidates()}

    @app.post("/api/v1/telegram/test")
    def telegram_test() -> dict[str, bool]:
        telegram = _get_configured_telegram(database)
        if not telegram["enabled"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Telegram 알림이 비활성화되어 있습니다"
            )
        TelegramClient(
            str(telegram["bot_token"]),
            str(telegram["chat_id"]),
            base_settings.request_timeout_seconds,
        ).send_message("✅ MovieMax 관리자 콘솔의 Telegram 연결이 정상입니다.")
        return {"sent": True}

    @app.post("/api/v1/outbox/requeue-dead")
    def requeue_dead() -> dict[str, int]:
        return {"requeued": database.requeue_dead()}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            _ASSETS / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/activity")
    def activity_page() -> FileResponse:
        return FileResponse(
            _ASSETS / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    app.mount(
        "/assets",
        StaticFiles(directory=_ASSETS),
        name="assets",
    )
    return app


def run_console_web(settings: ConsoleSettings) -> None:
    uvicorn.run(
        create_app(settings),
        host=settings.web_host,
        port=settings.web_port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


def console_web_health(settings: ConsoleSettings) -> dict[str, Any]:
    with urlopen(
        f"http://127.0.0.1:{settings.web_port}/healthz",
        timeout=5,
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"Console web returned HTTP {response.status}")
        body = json.loads(response.read().decode("utf-8"))
    if body.get("status") != "ok":
        raise RuntimeError("Console web health response is invalid")
    return body
