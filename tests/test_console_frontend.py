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

    assert "/assets/refined.css?v=20260727-3" in page
    assert "/assets/styles.css?v=20260727-3" in page
    assert "/assets/app.js?v=20260727-3" in page
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
    assert "showBrowserNotification(" in script
    assert "showInPageAlert(" in script
    assert 'bookingLink.textContent = "예매하기"' in script
    assert "monitorIncreaseAlerts().catch(reportBackgroundError)" in script


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
    assert 'id="activityFullList" aria-live="polite"' in page
    assert "font-size: clamp(21px, 2.2vw, 28px)" in stylesheet
    assert ".activity-page-statuses" in stylesheet
    assert ".activity-new-records[hidden]" in stylesheet

    assert "const liveSyncIntervalMs = 3000" in script
    assert "async function syncFullActivityLive()" in script
    assert "async function syncLiveView" in script
    assert "if (page !== 1)" in script
    assert "showPendingActivityRecords(items)" in script
    assert "if (contentChanged) renderActivityPage()" in script
    assert "if (targetId) await loadRecentAlerts(targetId)" in script
    assert "syncLiveView().catch(reportBackgroundError);" in script
    assert "monitorIncreaseAlerts().catch(reportBackgroundError);" in script
    assert "}, liveSyncIntervalMs);" in script
    assert 'document.addEventListener("visibilitychange"' in script
