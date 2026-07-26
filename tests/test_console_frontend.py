import re
from pathlib import Path

ASSET_DIR = Path(__file__).parents[1] / "src" / "moviemax" / "web_assets"


def test_dashboard_requests_and_renders_only_seat_increase_alerts() -> None:
    script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
    page = (ASSET_DIR / "index.html").read_text(encoding="utf-8")

    assert 'kind: "seat_increases"' in script
    assert '.filter((item) => item.kind === "seat_increases"' in script
    assert '"seat-moved-down"' not in script
    assert "최근 잔여석 증가가 없습니다" in script
    assert "회차별 기준 이상 늘어난 잔여석만 표시합니다" in page


def test_dashboard_keeps_absolute_and_live_relative_observation_times() -> None:
    script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")

    assert "function relativeTimeText" in script
    assert "function updateRelativeTimes" in script
    assert 'document.querySelectorAll("[data-relative-time]")' in script
    assert 'class="activity-time"' in script
    assert 'class="activity-age" data-relative-time=' in script
    assert '"알림 미선택 · 좌석 조회됨"' in script
    assert "updateRelativeTimes(now)" in script


def test_rectilinear_controls_share_one_height_and_have_safe_breakpoints() -> None:
    stylesheet = (ASSET_DIR / "styles.css").read_text(encoding="utf-8")

    assert "--control-height: 40px" in stylesheet
    assert ".target-toolbar > *" in stylesheet
    assert ".screening-actions .button" in stylesheet
    assert "height: var(--control-height)" in stylesheet
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in stylesheet
    assert "@media (max-width: 900px)" in stylesheet
    assert "@media (max-width: 700px)" in stylesheet
    assert "border-radius: 2px" in stylesheet


def test_dashboard_loads_spacious_refined_visual_layer() -> None:
    page = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (ASSET_DIR / "refined.css").read_text(encoding="utf-8")

    assert "/assets/refined.css?v=20260727-4" in page
    assert "/assets/styles.css?v=20260727-4" in page
    assert "/assets/app.js?v=20260727-4" in page
    assert 'class="product-brand"' in page
    assert "--page-gutter: clamp(18px, 2vw, 32px)" in stylesheet
    assert "--surface-strong: #171a17" in stylesheet
    assert ".activity-view" in stylesheet
    assert "@media (max-width: 520px)" in stylesheet
    assert "@media (prefers-reduced-motion: reduce)" in stylesheet


def test_overview_cards_share_a_desktop_row_height() -> None:
    stylesheet = (ASSET_DIR / "refined.css").read_text(encoding="utf-8")

    assert ".recent-panel,\n.operations-panel {\n  height: 100%;" in stylesheet
    assert "@media (max-width: 1120px)" in stylesheet
    assert ".recent-panel,\n  .operations-panel {\n    height: auto;" in stylesheet


def test_open_dashboard_monitors_and_announces_new_seat_increases() -> None:
    script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")

    assert "async function monitorIncreaseAlerts()" in script
    assert 'kind: "seat_increases"' in script
    assert "showInPageAlert(" in script
    assert "new Notification(" not in script
    assert 'bookingLink.textContent = "예매하기"' in script
    assert "monitorIncreaseAlerts().catch(reportBackgroundError)" in script


def test_web_push_requires_explicit_opt_in_and_uses_a_service_worker() -> None:
    page = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
    script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
    worker = (ASSET_DIR / "service-worker.js").read_text(encoding="utf-8")
    manifest = (ASSET_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
    stylesheet = (ASSET_DIR / "refined.css").read_text(encoding="utf-8")

    assert (
        '<link rel="manifest" href="/manifest.webmanifest" '
        'crossorigin="use-credentials">'
    ) in page
    assert 'class="notification-channel-list" role="group"' in page
    assert 'id="webPushMiniAction"' in page
    assert 'id="enableWebPush"' in page
    assert 'id="testWebPush"' in page
    assert 'id="disableWebPush"' in page
    assert "Notification.requestPermission()" in script
    assert (
        'addEventListener("click", () => requestNotificationPermission()' not in script
    )
    assert 'navigator.serviceWorker.register("/service-worker.js"' in script
    assert "registration.pushManager.subscribe" in script
    assert 'method: "PUT"' in script
    assert 'method: "DELETE"' in script
    assert "serverSynced: false" in script
    assert "serverInactive: false" in script
    assert "syncSuppressed: false" in script
    assert "webPushPendingEndpointStorageKey" in script
    assert "Promise.allSettled" in script
    assert "error.status = response.status" in script
    assert "[404, 410].includes(Number(error?.status))" in script
    assert "function applyWebPushSubscriptionResponse(result)" in script
    assert "subscriptionStatus.active === true" in script
    assert "subscriptionStatus.requires_resubscribe === true" in script
    assert "async function replaceInactiveWebPushSubscription" in script
    assert 'self.addEventListener("push"' in worker
    assert 'self.addEventListener("notificationclick"' in worker
    assert "self.registration.showNotification" in worker
    assert '"display": "standalone"' in manifest
    assert ".web-push-actions .button" in stylesheet
    assert "height: var(--control-height)" in stylesheet
    assert ".notification-channel-list > .telegram-mini" in stylesheet
    assert "border-left: 0" in stylesheet


def test_target_delete_ui_warns_that_all_related_history_is_removed() -> None:
    page = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
    script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="openDeleteTarget"' in page
    assert 'id="deleteTargetDialog"' in page
    assert "모든 상영 회차, 좌석 변동 이력, 알림 기록" in page
    assert "async function deleteCurrentTarget(event)" in script
    assert 'method: "DELETE"' in script
    assert "seat_history" in script
    assert "state.targets = state.targets.filter" in script
    assert "삭제는 완료됐지만 최신 화면을 불러오지 못했습니다" in script
    assert "reportBackgroundError(refreshError)" in script


def test_target_dialog_requires_an_exact_movie_format_selection() -> None:
    page = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
    script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
    stylesheet = (ASSET_DIR / "styles.css").read_text(encoding="utf-8")

    assert re.search(
        r'<select id="formatSelect"[^>]*\brequired\b[^>]*\bdisabled\b', page
    )
    assert 'id="selectionPreview" role="status" aria-live="polite"' in page
    assert "catalogMovies: []" in script
    assert "function loadFormats(movieNo)" in script
    assert "function selectedCatalogFormat()" in script
    assert (
        'byId("movieSelect").addEventListener("change", (event) => loadFormats(event.target.value))'
        in script
    )
    assert (
        'byId("formatSelect").addEventListener("change", updateSelectionPreview)'
        in script
    )
    assert "format_code: String(format.format_code)" in script
    assert "format_name: String(format.format_name)" in script
    assert ".target-format-field {\n  grid-column: 1 / -1;\n}" in stylesheet


def test_dashboard_identifies_targets_and_alerts_by_selected_format() -> None:
    page = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
    script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")

    assert "function targetFormatLabel(target)" in script
    assert "screening.format_name" in script
    assert "포맷 / 상영관" in script
    assert "상영 포맷 미확인" in script
    assert "새 상영 회차 감지" in script
    assert "극장과 영화, 추적할 상영 포맷을 차례로 선택하세요." in page
    for legacy_copy in (
        "IMAX 감지 콘솔",
        "IMAX 감지 현황",
        "IMAX watch console",
        "새 IMAX 알림",
        "IMAX 상영 회차",
        "IMAX 상영 영화",
        "현재 확인되는 IMAX 영화",
    ):
        assert legacy_copy not in page + script


def test_activity_page_has_compact_live_header_and_realtime_updates() -> None:
    page = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
    script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
    stylesheet = (ASSET_DIR / "styles.css").read_text(encoding="utf-8")

    assert 'class="activity-page-brand"' in page
    assert 'id="activityPollCountdown" role="timer"' in page
    assert 'id="activityWorkerLabel" role="status" aria-live="polite"' in page
    assert 'id="activityNewRecords"' in page and " hidden" in page
    assert 'id="showLatestActivity"' in page
    assert 'id="activityFullList"' in page
    assert 'id="activityLiveUpdateStatus" role="status" aria-live="polite"' in page
    assert "font-size: clamp(21px, 2.2vw, 28px)" in stylesheet
    assert ".activity-page-statuses" in stylesheet
    assert ".activity-new-records[hidden]" in stylesheet

    assert "const liveSyncIntervalMs = 3000" in script
    assert "async function syncFullActivityLive()" in script
    assert "async function syncLiveView" in script
    assert "if (page !== 1)" in script
    assert "showPendingActivityRecords(items)" in script
    assert "if (contentChanged) {" in script
    assert "announceActivityLiveUpdate(" in script
    assert "if (targetId) await loadRecentAlerts(targetId)" in script
    assert "await Promise.all([loadActivityStatus(), syncFullActivityLive()])" in script
    assert "if (workerLabel && workerLabel.textContent !== countdown.label)" in script
    assert "restoreActivityListFocus(focusMarker)" in script
    assert "syncLiveView().catch(reportBackgroundError);" in script
    assert "monitorIncreaseAlerts().catch(reportBackgroundError);" in script
    assert "}, liveSyncIntervalMs);" in script
    assert 'document.addEventListener("visibilitychange"' in script
