"use strict";

const state = {
  targets: [],
  selectedTargetId: null,
  screenings: [],
  activity: [],
  recentAlerts: [],
  telegram: {},
  webPush: {
    configured: false,
    public_key: "",
    subscription_count: 0,
    supported: null,
    registration: null,
    subscription: null,
    serverSynced: false,
    serverInactive: false,
    syncSuppressed: false,
    pendingEndpoint: "",
    syncGeneration: 0,
    loading: false,
  },
  workerOk: null,
  catalog: null,
  catalogMovies: [],
  filter: "all",
  refreshing: false,
  refreshRequest: null,
  serverClockOffsetMs: 0,
  pollSettingsDirtyTargetId: null,
  bulkThresholdDirtyTargetId: null,
  screeningThresholdDrafts: new Map(),
  screeningSeatSnapshot: new Map(),
  recentAlertsRenderSignature: "",
  seenAlertIds: new Set(),
  alertsInitialized: false,
  alertMonitorRequestId: 0,
  alertMonitorInFlight: false,
  dashboardRequests: { bootstrap: 0, screenings: 0, alerts: 0 },
  expandedScreenings: new Set(),
  expandedActivities: new Set(),
  activityLog: {
    items: [],
    page: 1,
    pageCursors: [null],
    nextCursor: null,
    hasMore: false,
    loading: false,
    requestId: 0,
    filters: { targetId: "", screeningId: "", kind: "" },
  },
  liveSync: {
    inFlight: false,
    activityHeadId: null,
    activityFilterKey: "",
    pendingHeadId: null,
    pendingNewCount: 0,
    statusRequestId: 0,
  },
};

const byId = (id) => document.getElementById(id);
const csrfHeader = { "X-MovieMax-CSRF": "1" };
const activityPath = window.location.pathname.replace(/\/+$/, "") === "/activity";
const liveSyncIntervalMs = 3000;
let toastTimer;
let dialogReturnFocus = null;

function focusMarker() {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return null;
  for (const key of ["targetId", "watchId", "thresholdId", "filter", "chatId"]) {
    if (active.dataset[key]) return { key, value: active.dataset[key] };
  }
  return active.id ? { key: "id", value: active.id } : null;
}

function restoreFocus(marker) {
  if (!marker) return;
  let element = null;
  if (marker.key === "id") {
    element = byId(marker.value);
  } else {
    element = [...document.querySelectorAll(`[data-${marker.key.replace(/[A-Z]/g, (value) => `-${value.toLowerCase()}`)}]`)]
      .find((candidate) => candidate.dataset[marker.key] === marker.value);
  }
  if (element instanceof HTMLElement) element.focus({ preventScroll: true });
}

function restoreFocusIfLost(marker) {
  if (document.activeElement === document.body || document.activeElement === null) {
    restoreFocus(marker);
  }
}

function showDialog(dialog, initialFocus) {
  dialogReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  dialog.showModal();
  requestAnimationFrame(() => initialFocus?.focus());
}

function setButtonBusy(button, busy) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function apiErrorMessage(body, status) {
  const detail = body?.detail ?? body?.error;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object") {
        const field = Array.isArray(item.loc)
          ? item.loc.filter((part) => part !== "body").join(".")
          : "";
        const message = item.msg || item.message || JSON.stringify(item);
        return field ? `${field}: ${message}` : message;
      }
      return String(item);
    }).filter(Boolean);
    if (messages.length) return messages.join(" · ");
  }
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return `요청 실패 (${status})`;
}

async function api(path, options = {}) {
  const mutation = options.method && options.method !== "GET";
  const headers = { ...(options.headers || {}) };
  if (mutation) Object.assign(headers, csrfHeader);
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(apiErrorMessage(body, response.status));
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

function showToast(message, error = false) {
  const toast = byId("toast");
  toast.setAttribute("role", error ? "alert" : "status");
  toast.textContent = message;
  toast.classList.toggle("is-error", error);
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 3200);
}

function showInPageAlert(message, url) {
  let banner = document.getElementById("liveAlertBanner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "liveAlertBanner";
    banner.setAttribute("role", "alert");
    banner.setAttribute("aria-live", "assertive");
    document.body.appendChild(banner);
  }
  const copy = document.createElement("span");
  copy.textContent = message;
  banner.replaceChildren(copy);
  if (url) {
    const bookingLink = document.createElement("a");
    bookingLink.href = url;
    bookingLink.target = "_blank";
    bookingLink.rel = "noopener noreferrer";
    bookingLink.textContent = "예매하기";
    banner.appendChild(bookingLink);
  }
  banner.classList.add("is-visible");
  clearTimeout(banner._timer);
  banner._timer = setTimeout(() => banner.classList.remove("is-visible"), 6000);
}

function notifyIncreaseAlert(alert) {
  const entries = activityEntries(alert);
  const entry = entries[0] || { screening: {}, previous: {}, changes: [] };
  const screening = recordOrEmpty(entry.screening);
  const seatChange = activitySeatChange(alert, entry);
  const movieName = screening.movie_name || "영화 정보 미확인";
  const formatName = screening.format_name || "상영 포맷 미확인";
  const session = activitySessionText(screening);
  const deltaText = seatChange ? `${seatChange.before} → ${seatChange.after}석` : "잔여석 변동";
  const bookingUrl = activityBookingUrl(alert, screening);
  showInPageAlert(`잔여석 증가 감지 · ${movieName} · ${formatName} · ${session} · ${deltaText}`, bookingUrl);
}

async function monitorIncreaseAlerts() {
  if (state.alertMonitorInFlight) return;
  state.alertMonitorInFlight = true;
  const requestId = state.alertMonitorRequestId + 1;
  state.alertMonitorRequestId = requestId;
  const params = new URLSearchParams({
    limit: "50",
    kind: "seat_increases",
    notifications_only: "true",
  });
  try {
    const data = await api(`/api/v1/activity?${params.toString()}`);
    if (requestId !== state.alertMonitorRequestId) return;
    const alerts = Array.isArray(data.items)
      ? data.items
      : Array.isArray(data.activity) ? data.activity : [];
    if (state.alertsInitialized) {
      alerts
        .filter((item) => item.kind === "seat_increases" && !state.seenAlertIds.has(String(item.id)))
        .reverse()
        .forEach(notifyIncreaseAlert);
    }
    alerts.forEach((item) => state.seenAlertIds.add(String(item.id)));
    state.alertsInitialized = true;
  } finally {
    state.alertMonitorInFlight = false;
  }
}

function humanDateTime(value) {
  if (!value) return "아직 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(date);
}

function timestampMs(value) {
  if (!value) return null;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function durationText(milliseconds) {
  const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours) return `${hours}시간 ${String(minutes).padStart(2, "0")}분 ${String(seconds).padStart(2, "0")}초`;
  if (minutes) return `${minutes}분 ${String(seconds).padStart(2, "0")}초`;
  return `${seconds}초`;
}

function relativeTimeText(value, now = Date.now() + state.serverClockOffsetMs) {
  const observedAt = timestampMs(value);
  if (observedAt === null) return "시간 미확인";
  const elapsedSeconds = Math.max(0, Math.floor((now - observedAt) / 1000));
  if (elapsedSeconds < 60) return `${elapsedSeconds}초 전`;
  const elapsedMinutes = Math.floor(elapsedSeconds / 60);
  if (elapsedMinutes < 60) return `${elapsedMinutes}분 전`;
  const elapsedHours = Math.floor(elapsedMinutes / 60);
  if (elapsedHours < 24) return `${elapsedHours}시간 전`;
  return `${Math.floor(elapsedHours / 24)}일 전`;
}

function updateRelativeTimes(now = Date.now() + state.serverClockOffsetMs) {
  document.querySelectorAll("[data-relative-time]").forEach((element) => {
    const text = relativeTimeText(element.dataset.relativeTime, now);
    if (element.textContent !== text) element.textContent = text;
  });
}

function telegramFeedback(message = "", kind = "info") {
  const feedback = byId("telegramFeedback");
  feedback.hidden = !message;
  feedback.textContent = message;
  feedback.className = `form-feedback is-${kind}`;
  feedback.setAttribute("role", kind === "error" ? "alert" : "status");
}

function errorMessage(error) {
  return error instanceof Error && error.message ? error.message : "처리 중 오류가 발생했습니다.";
}

function displayDate(value) {
  if (!/^\d{8}$/.test(value || "")) return value;
  const date = new Date(`${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6,8)}T12:00:00+09:00`);
  return new Intl.DateTimeFormat("ko-KR", { month: "long", day: "numeric", weekday: "short" }).format(date);
}

function displayTime(value) {
  if (!/^\d{4}$/.test(value || "")) return value || "—";
  return `${value.slice(0, 2)}:${value.slice(2)}`;
}

function selectedTarget() {
  return state.targets.find((target) => target.id === state.selectedTargetId) || null;
}

function targetFormatLabel(target) {
  return target?.format_name || target?.format_keyword || "상영 포맷 미확인";
}

function formatBadge(formatName) {
  const normalized = String(formatName || "").toUpperCase();
  if (normalized.includes("IMAX")) return "IM";
  if (normalized.includes("4DX")) return "4D";
  if (normalized.includes("SCREENX")) return "SX";
  const compact = (normalized.match(/[A-Z0-9]+/g) || []).join("");
  return compact.slice(0, 2) || "CG";
}

function refreshPhase(target) {
  if (!target?.enabled) return "disabled";
  if (target.state === "running") return "running";
  if (target.refresh_requested_at) return "queued";
  return "idle";
}

function pollContextText(target) {
  const interval = Number(target?.poll_interval_seconds);
  const jitter = Number(target?.poll_jitter_seconds ?? 0);
  const parts = [Number.isFinite(interval) ? `기본 간격 ${interval}초` : null];
  if (Number.isFinite(interval) && Number.isFinite(jitter)) {
    parts.push(`최대 추가 지연 ${jitter}초`, `실효 범위 ${interval}~${interval + jitter}초`);
  }
  const startedAt = timestampMs(target?.last_started_at);
  const succeededAt = timestampMs(target?.last_success_at);
  if (startedAt !== null && succeededAt !== null && succeededAt >= startedAt) {
    const seconds = (succeededAt - startedAt) / 1000;
    parts.push(`직전 조회 ${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}초`);
  }
  return parts.filter(Boolean).join(" · ");
}

function pollSettingsFeedback(message = "", kind = "info") {
  const feedback = byId("pollSettingsFeedback");
  feedback.hidden = !message;
  feedback.textContent = message;
  feedback.className = `poll-settings-feedback is-${kind}`;
  feedback.setAttribute("role", kind === "error" ? "alert" : "status");
}

function bulkThresholdFeedback(message = "", kind = "info") {
  const feedback = byId("bulkThresholdFeedback");
  feedback.hidden = !message;
  feedback.textContent = message;
  feedback.className = `poll-settings-feedback is-${kind}`;
  feedback.setAttribute("role", kind === "error" ? "alert" : "status");
}

function renderBulkThreshold(target) {
  const input = byId("bulkThreshold");
  const button = byId("applyBulkThreshold");
  const tracked = state.screenings.filter((item) => item.watched);
  const thresholds = [...new Set(tracked
    .map((item) => Number(item.seat_change_threshold))
    .filter((value) => Number.isInteger(value) && value >= 1))];
  if (state.bulkThresholdDirtyTargetId !== target.id) {
    input.value = thresholds.length === 1 ? String(thresholds[0]) : "";
  }
  input.placeholder = thresholds.length > 1 ? "기준이 서로 다름" : "예: 3";
  input.disabled = tracked.length === 0;
  button.disabled = tracked.length === 0 || !input.checkValidity() || !input.value;
  byId("bulkThresholdSummary").textContent = tracked.length
    ? `알림 회차 ${tracked.length}개에 일괄 적용`
    : "먼저 회차별 알림을 켜세요";
}

function updatePollSettingsPreview({ announce = false } = {}) {
  const target = selectedTarget();
  const intervalInput = byId("pollInterval");
  const jitterInput = byId("pollJitter");
  const interval = intervalInput.valueAsNumber;
  const jitter = jitterInput.valueAsNumber;
  const valid = intervalInput.checkValidity() && jitterInput.checkValidity()
    && Number.isInteger(interval) && Number.isInteger(jitter);
  if (valid) {
    byId("pollRange").textContent = `현재 실효 범위 ${interval}~${interval + jitter}초 (무작위 추가 0~${jitter}초)`;
  } else {
    byId("pollRange").textContent = "기본 간격 5~3600초, 최대 추가 지연 0~300초를 입력하세요.";
  }
  const changed = Boolean(target) && valid
    && (interval !== Number(target.poll_interval_seconds)
      || jitter !== Number(target.poll_jitter_seconds ?? 0));
  state.pollSettingsDirtyTargetId = changed ? target.id : null;
  byId("savePollSettings").disabled = !changed;
  if (announce) {
    pollSettingsFeedback(
      valid ? (changed ? "변경사항이 아직 저장되지 않았습니다." : "현재 저장된 조회 간격과 같습니다.") : "허용 범위 안의 정수를 입력하세요.",
      valid ? "info" : "error",
    );
  }
}

function renderPollSettings(target) {
  if (state.pollSettingsDirtyTargetId !== target.id) {
    byId("pollInterval").value = String(target.poll_interval_seconds);
    byId("pollJitter").value = String(target.poll_jitter_seconds ?? 0);
  }
  updatePollSettingsPreview();
}

function pollStatusText(target, now = Date.now()) {
  if (!target) return "감시 대상을 선택하세요";
  const phase = refreshPhase(target);
  const context = pollContextText(target);
  const suffix = context ? ` · ${context}` : "";
  if (phase === "disabled") return `감지 중지됨 · 감지 실행을 켜면 다시 조회합니다${suffix}`;
  if (phase === "running") {
    const started = target.last_started_at ? ` · 시작 ${humanDateTime(target.last_started_at)}` : "";
    return `CGV 조회 중${started}${suffix}`;
  }
  if (phase === "queued") {
    const requestedAt = timestampMs(target.refresh_requested_at);
    const elapsed = requestedAt === null ? "" : ` · 요청 후 ${durationText(now - requestedAt)} 경과`;
    return `즉시 조회 대기 중${elapsed}${suffix}`;
  }
  const nextPollAt = timestampMs(target.next_poll_at);
  if (nextPollAt === null) return `다음 조회를 곧 시작합니다${suffix}`;
  if (nextPollAt <= now) return `다음 조회 대기 중 · 예정 ${humanDateTime(target.next_poll_at)}${suffix}`;
  return `다음 조회까지 ${durationText(nextPollAt - now)} · 예정 ${humanDateTime(target.next_poll_at)}${suffix}`;
}

function syncRefreshButton(target) {
  const button = byId("refreshNow");
  const label = byId("refreshLabel");
  const phase = refreshPhase(target);
  const requestBusy = state.refreshing;
  button.disabled = !target || phase !== "idle" || requestBusy;
  button.setAttribute("aria-busy", String(requestBusy || phase === "running"));
  if (!target) {
    label.textContent = "↻ 새로 조회";
    button.title = "감시 대상을 먼저 선택하세요";
  } else if (phase === "disabled") {
    label.textContent = "↻ 감지 중지됨";
    button.title = "감지 실행을 켠 뒤 조회할 수 있습니다";
  } else if (requestBusy) {
    label.textContent = "↻ 요청 중…";
    button.title = "즉시 조회를 요청하고 있습니다";
  } else if (phase === "queued") {
    label.textContent = "↻ 조회 대기";
    button.title = "즉시 조회 요청이 워커 대기열에 있습니다";
  } else if (phase === "running") {
    label.textContent = "↻ 조회 중…";
    button.title = "CGV 상영시간표를 조회하고 있습니다";
  } else {
    label.textContent = "↻ 새로 조회";
    button.title = "현재 대상을 즉시 조회합니다";
  }
}

function reconcileRefreshRequest() {
  const request = state.refreshRequest;
  if (!request) return;
  const target = state.targets.find((item) => item.id === request.targetId);
  if (!target) {
    state.refreshRequest = null;
    return;
  }
  if (refreshPhase(target) !== "idle") return;
  const successAt = timestampMs(target.last_success_at);
  const failureAt = timestampMs(target.last_failure_at);
  if (successAt !== null && successAt >= request.requestedAt) {
    showToast(`즉시 조회를 완료했습니다 · ${humanDateTime(target.last_success_at)}`);
    state.refreshRequest = null;
  } else if (failureAt !== null && failureAt >= request.requestedAt) {
    showToast(`즉시 조회에 실패했습니다 · ${target.last_error || "원인을 확인하세요"}`, true);
    state.refreshRequest = null;
  }
}

function updateLiveTimes() {
  const target = selectedTarget();
  const now = Date.now() + state.serverClockOffsetMs;
  if (target) {
    byId("pollStatus").textContent = pollStatusText(target, now);
    const metric = byId("metricUpdated");
    metric.textContent = humanDateTime(target.last_success_at);
    if (target.last_success_at) metric.setAttribute("datetime", target.last_success_at);
    else metric.removeAttribute("datetime");
  }
  updateRelativeTimes(now);
  updateActivityPollCountdown(now);
  syncRefreshButton(target);
}

function renderWorker(status) {
  state.workerOk = status?.worker_ok === true
    ? true
    : status?.worker_ok === false ? false : null;
  const chip = byId("workerChip");
  chip.classList.toggle("is-ok", status?.worker_ok === true);
  chip.classList.toggle("is-error", status?.worker_ok === false);
  const label = status?.worker_ok ? "감지 정상" : "감지 확인 필요";
  if (byId("workerLabel").textContent !== label) byId("workerLabel").textContent = label;
}

function renderTargets() {
  const container = byId("targetList");
  if (!state.targets.length) {
    container.innerHTML = '<p class="activity-empty">아직 감시 대상이 없습니다.</p>';
    return;
  }
  container.innerHTML = state.targets.map((target) => {
    const active = target.id === state.selectedTargetId;
    const dotClass = target.last_error ? "is-error" : target.enabled ? "is-on" : "";
    const statusLabel = target.last_error ? "조회 오류" : target.enabled ? "감지 실행 중" : "감지 중지";
    const formatName = targetFormatLabel(target);
    return `
      <button class="target-item ${active ? "is-active" : ""}" data-target-id="${target.id}" type="button" aria-pressed="${active}" aria-label="${escapeHtml(target.site_name)} ${escapeHtml(target.movie_name)} ${escapeHtml(formatName)} 선택, ${statusLabel}">
        <span class="target-glyph">${escapeHtml(formatBadge(formatName))}</span>
        <span class="target-copy">
          <strong>${escapeHtml(target.movie_name)}</strong>
          <small>${escapeHtml(target.site_name)} · ${escapeHtml(formatName)}</small>
        </span>
        <span class="mini-dot ${dotClass}" aria-hidden="true"></span>
      </button>`;
  }).join("");
}

function screeningState(item) {
  if (item.control_yn === "Y") return { label: "예매 준비중", klass: "is-preparing" };
  if (item.free_seats <= 0) return { label: "매진", klass: "" };
  return { label: "예매 가능", klass: "is-open" };
}

function saleStatus(controlYn, freeSeats) {
  if (controlYn === "Y") return "예매 준비중";
  return Number(freeSeats) > 0 ? "예매 가능" : "매진";
}

function latestHistorySummary(history) {
  if (!Array.isArray(history) || !history.length) return "관측 이력 없음";
  const latest = history.at(-1);
  if (history.length === 1) {
    return `최초 관측 · ${humanDateTime(latest.observed_at)}`;
  }
  const previous = history.at(-2);
  const changes = [];
  const seatDelta = Number(latest.free_seats) - Number(previous.free_seats);
  const totalDelta = Number(latest.total_seats) - Number(previous.total_seats);
  if (previous.control_yn !== latest.control_yn) {
    changes.push(latest.control_yn === "N" ? "예매 오픈" : `상태 ${previous.control_yn}→${latest.control_yn}`);
  }
  if (seatDelta) changes.push(`잔여석 ${seatDelta > 0 ? "+" : ""}${seatDelta}`);
  if (totalDelta) changes.push(`전체 좌석 ${totalDelta > 0 ? "+" : ""}${totalDelta}`);
  if (!changes.length) changes.push("좌석·상태 외 정보 변경");
  return `최근 ${changes.join(" · ")} · ${humanDateTime(latest.observed_at)}`;
}

function detailPairsHtml(pairs, className = "detail-pairs") {
  return `<dl class="${className}">${pairs.map(([label, value]) => `
    <div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "—")}</dd></div>`).join("")}</dl>`;
}

function bookingButton(url, label = "예매하기") {
  if (!url) return "";
  try {
    const parsed = new URL(url, window.location.origin);
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("unsupported URL");
    if (label === "예매하기") {
      return `<a class="button booking-button" href="${escapeHtml(parsed.href)}" target="_blank" rel="noopener noreferrer">예매하기</a>`;
    }
    return `<a class="button booking-button" href="${escapeHtml(parsed.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
  } catch (_error) {
    return "";
  }
}

function renderDashboard() {
  const target = selectedTarget();
  byId("emptyState").classList.toggle("is-hidden", Boolean(target));
  byId("dashboardContent").classList.toggle("is-hidden", !target);
  if (!target) return;

  const formatName = targetFormatLabel(target);
  byId("targetKicker").textContent = `CGV ${target.site_name} · ${formatName}`;
  byId("targetTitle").textContent = target.movie_name;
  byId("scheduleTitle").textContent = `${formatName} 상영 회차`;
  const pollJitter = Number(target.poll_jitter_seconds ?? 0);
  byId("targetMeta").textContent = `극장 ${target.site_no} · 영화 ${target.movie_no} · 기본 ${target.poll_interval_seconds}초 + 무작위 최대 ${pollJitter}초`;
  byId("targetEnabled").checked = Boolean(target.enabled);
  byId("notifyNew").checked = Boolean(target.notify_new);
  byId("autoTrackNew").checked = Boolean(target.auto_track_new);
  renderPollSettings(target);

  const open = state.screenings.filter((item) => item.control_yn !== "Y");
  const tracked = state.screenings.filter((item) => item.watched);
  const seats = open.reduce((sum, item) => sum + Math.max(0, item.free_seats || 0), 0);
  byId("metricOpen").textContent = open.length;
  byId("metricOpenSub").textContent = `전체 ${state.screenings.length}회차`;
  byId("metricTracked").textContent = tracked.length;
  byId("metricSeats").textContent = seats.toLocaleString("ko-KR");
  const metricUpdated = byId("metricUpdated");
  metricUpdated.textContent = humanDateTime(target.last_success_at);
  if (target.last_success_at) metricUpdated.setAttribute("datetime", target.last_success_at);
  else metricUpdated.removeAttribute("datetime");
  byId("metricError").textContent = target.last_error || "정상";

  renderBulkThreshold(target);
  updateLiveTimes();
  renderSchedule();
}

function renderSchedule() {
  const target = selectedTarget();
  const status = byId("scheduleStatus");
  if (!target) return;

  let rows = [...state.screenings];
  if (state.filter === "tracked") rows = rows.filter((item) => item.watched);
  if (state.filter === "available") rows = rows.filter((item) => item.control_yn !== "Y" && item.free_seats > 0);
  const statusText = target.last_error
    ? `최근 조회 오류 · ${target.last_error}`
    : `${rows.length}개 회차 표시 · 알림 선택 ${state.screenings.filter((item) => item.watched).length}개 · 모든 표시 좌석은 ${humanDateTime(target.last_success_at)} 조회값`;
  if (status.textContent !== statusText) status.textContent = statusText;

  if (!rows.length) {
    byId("scheduleList").innerHTML = '<div class="schedule-empty">조건에 맞는 상영 회차가 없습니다.<br>감지 실행 후 잠시 기다리거나 지금 조회를 눌러주세요.</div>';
    return;
  }

  const motionById = new Map();
  for (const item of state.screenings) {
    const key = String(item.id);
    const current = Number(item.free_seats) || 0;
    const previous = state.screeningSeatSnapshot.get(key);
    if (Number.isFinite(previous) && current > previous) {
      motionById.set(key, "seat-moved-up");
    }
    state.screeningSeatSnapshot.set(key, current);
  }

  const groups = rows.reduce((result, item) => {
    if (!result.has(item.screening_date)) result.set(item.screening_date, []);
    result.get(item.screening_date).push(item);
    return result;
  }, new Map());
  byId("scheduleList").innerHTML = [...groups.entries()].map(([date, items]) => `
    <section class="date-group">
      <div class="date-heading"><strong>${escapeHtml(displayDate(date))}</strong><span>${items.length}회차</span></div>
      ${items.map((item) => renderScreening(item, motionById.get(String(item.id)) || "")).join("")}
    </section>`).join("");
}

function renderScreening(item, motionClass = "") {
  const currentState = screeningState(item);
  const totalSeats = Math.max(0, Number(item.total_seats) || 0);
  const freeSeats = Math.max(0, Number(item.free_seats) || 0);
  const seatsClass = freeSeats > 0 ? "has-seats" : "is-sold";
  const seatLabel = item.control_yn === "Y" ? "준비중" : freeSeats > 0 ? `예매 가능 · ${freeSeats}석` : "매진";
  const history = latestHistorySummary(item.history);
  const progressMax = Math.max(1, totalSeats, freeSeats);
  const threshold = Math.max(1, Number(item.seat_change_threshold) || 1);
  const thresholdKey = String(item.id);
  const thresholdDraft = state.screeningThresholdDrafts.get(thresholdKey);
  const thresholdValue = thresholdDraft ?? String(threshold);
  const thresholdHelp = thresholdDraft !== undefined
    ? "저장 전 변경"
    : item.watched ? `+${threshold}석부터 알림` : "알림 선택 시 적용";
  const lastObservedAt = item.last_seen_at || item.history?.at(-1)?.observed_at || "";
  const observationLabel = item.watched ? "알림 선택 · 좌석 조회" : "알림 미선택 · 좌석 조회됨";
  const rowLabel = `${displayDate(item.screening_date)} ${displayTime(item.start_time)}, ${item.screen_name}, ${currentState.label}, 잔여 ${freeSeats}석, 전체 ${totalSeats}석`;
  return `
    <article class="screening-row ${item.watched ? "is-tracked" : ""} ${motionClass}" aria-label="${escapeHtml(rowLabel)}">
      <div class="screening-time"><strong>${escapeHtml(displayTime(item.start_time))}</strong><small>${escapeHtml(displayTime(item.end_time))}</small></div>
      <div class="screening-info"><strong>${escapeHtml(item.screen_name)}</strong><small>${escapeHtml(item.format_name)} · ${escapeHtml(item.sequence)}회차</small></div>
      <div class="seat-block">
        <strong class="${seatsClass}">${escapeHtml(seatLabel)} <small>/ ${totalSeats}석</small></strong>
        <progress max="${progressMax}" value="${freeSeats}" aria-label="잔여 ${freeSeats}석, 전체 ${totalSeats}석"></progress>
        <small class="screening-observation ${item.watched ? "is-selected" : ""}">${escapeHtml(observationLabel)} · <time datetime="${escapeHtml(lastObservedAt)}" data-relative-time="${escapeHtml(lastObservedAt)}">${escapeHtml(relativeTimeText(lastObservedAt))}</time></small>
        <small class="screening-history-summary">${escapeHtml(history)}</small>
      </div>
      <div class="threshold-setting">
        <label for="threshold-${escapeHtml(item.id)}">증가 알림 기준</label>
        <span class="threshold-editor">
          <input id="threshold-${escapeHtml(item.id)}" data-threshold-id="${escapeHtml(item.id)}" type="number" min="1" max="9007199254740991" step="1" inputmode="numeric" value="${escapeHtml(thresholdValue)}" aria-label="${escapeHtml(displayDate(item.screening_date))} ${escapeHtml(displayTime(item.start_time))} 잔여석 증가 알림 기준">
          <span aria-hidden="true">석</span>
          <button data-threshold-save-id="${escapeHtml(item.id)}" type="button">저장</button>
        </span>
        <small>${escapeHtml(thresholdHelp)}</small>
      </div>
      <div class="screening-actions">
        <button class="watch-button ${item.watched ? "is-on" : ""}" data-watch-id="${item.id}" data-watch-enabled="${item.watched ? "false" : "true"}" type="button" aria-pressed="${Boolean(item.watched)}" aria-label="${escapeHtml(displayDate(item.screening_date))} ${escapeHtml(displayTime(item.start_time))} 잔여석 증가 알림 ${item.watched ? "끄기" : "켜기"}">${item.watched ? "알림 끄기" : "증가 알림"}</button>
        ${bookingButton(item.booking_url, "예매")}
      </div>
      ${renderScreeningHistory(item)}
    </article>`;
}

function renderScreeningHistory(item) {
  const history = Array.isArray(item.history) ? item.history : [];
  const key = String(item.id);
  const open = state.expandedScreenings.has(key) ? " open" : "";
  const metadata = detailPairsHtml([
    ["상영 ID", item.id],
    ["대상 ID", item.target_id],
    ["상영 키", item.screening_key],
    ["현재 리비전", item.revision],
    ["최초 발견", humanDateTime(item.first_seen_at)],
    ["최근 확인", humanDateTime(item.last_seen_at)],
    ["알림 상태", item.watched ? "잔여석 증가 알림 켜짐" : "잔여석 증가 알림 꺼짐"],
    ["알림 기준", `증가 ${Math.max(1, Number(item.seat_change_threshold) || 1)}석 이상`],
  ], "detail-pairs history-meta");
  const rows = history.length
    ? history.map((entry, index) => {
      const previous = index > 0 ? history[index - 1] : null;
      const delta = previous ? Number(entry.free_seats) - Number(previous.free_seats) : null;
      const deltaText = delta === null ? "최초 관측" : delta > 0 ? `+${delta}석` : `${delta}석`;
      const deltaClass = delta > 0 ? "is-positive" : delta < 0 ? "is-negative" : "";
      return `
        <li class="history-row">
          <div>
            <time datetime="${escapeHtml(entry.observed_at || "")}">${escapeHtml(humanDateTime(entry.observed_at))}</time>
            <small>리비전 ${escapeHtml(entry.revision)} · ${escapeHtml(saleStatus(entry.control_yn, entry.free_seats))} · 제어값 ${escapeHtml(entry.control_yn)}</small>
          </div>
          <strong>${escapeHtml(entry.free_seats)} / ${escapeHtml(entry.total_seats)}석</strong>
          <span class="history-delta ${deltaClass}">${escapeHtml(deltaText)}</span>
        </li>`;
    }).join("")
    : '<li class="history-empty">아직 저장된 관측 이력이 없습니다.</li>';
  return `
    <details class="history-details" data-screening-history-id="${escapeHtml(key)}"${open}>
      <summary>상영·좌석 상세 <span>${escapeHtml(latestHistorySummary(history))} · ${history.length}건</span></summary>
      <div class="history-content">
        ${metadata}
        ${item.booking_url ? `<div class="detail-link-row">${bookingButton(item.booking_url)}</div>` : ""}
        <ol class="history-list">${rows}</ol>
      </div>
    </details>`;
}

function activityTitle(item) {
  const titles = {
    new_screenings: "새 상영 회차 감지",
    booking_opened: "예매 오픈",
    booking_closed: "예매 종료",
    seat_increases: "잔여석 증가",
    seat_decreases: "잔여석 감소",
    total_seats_changed: "전체 좌석 변경",
    screening_updated: "상영 정보 변경",
  };
  return titles[item.kind] || "알림 이벤트";
}

function recordOrEmpty(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function activityEntries(item) {
  const direct = recordOrEmpty(item.screening);
  if (Object.keys(direct).length) {
    return [{
      screening: direct,
      previous: recordOrEmpty(item.previous_screening),
      changes: Array.isArray(item.changes) ? item.changes : [],
    }];
  }

  const payload = recordOrEmpty(item.payload);
  if (Array.isArray(payload.screenings)) {
    return payload.screenings.filter((screening) => screening && typeof screening === "object")
      .map((screening) => ({ screening, previous: {}, changes: [] }));
  }
  if (Array.isArray(payload.changes)) {
    return payload.changes.filter((change) => change?.screening)
      .map((change) => ({
        screening: change.screening,
        previous: Number.isFinite(Number(change.previous_free_seats))
          ? { free_seats: Number(change.previous_free_seats) }
          : {},
        changes: Number.isFinite(Number(change.previous_free_seats))
          ? [{ field: "free_seats", before: Number(change.previous_free_seats), after: change.screening.free_seats }]
          : [],
      }));
  }
  return [];
}

function activityTimestamp(item) {
  return item.observed_at || item.created_at || item.notification?.created_at || "";
}

function activityBookingUrl(item, screening = {}) {
  return item.booking_url || screening.booking_url || "";
}

function activitySessionText(screening = {}) {
  const sequence = screening.sequence ? `${screening.sequence}회차` : "회차 미확인";
  const date = screening.screening_date ? displayDate(screening.screening_date) : "일자 미확인";
  const time = screening.start_time ? displayTime(screening.start_time) : "시간 미확인";
  return `${sequence} · ${date} ${time}`;
}

function activityHallText(screening = {}) {
  return screening.screen_name || (screening.screen_no ? `상영관 ${screening.screen_no}` : "상영관 미확인");
}

function activitySeatChange(item, entry) {
  const changes = Array.isArray(entry.changes) ? entry.changes : [];
  const change = changes.find((candidate) => candidate?.field === "free_seats");
  const beforeValue = change?.before ?? entry.previous?.free_seats;
  const afterValue = change?.after ?? entry.screening?.free_seats;
  const before = Number(beforeValue);
  const after = Number(afterValue);
  if (!["seat_increases", "seat_decreases"].includes(item.kind)
      || !Number.isFinite(before) || !Number.isFinite(after)) return null;
  return { before, after, delta: after - before };
}

function activityToneClass(item) {
  if (item.kind === "seat_increases") return "is-increase";
  if (item.kind === "seat_decreases") return "is-decrease";
  if (item.kind === "booking_closed") return "is-decrease";
  return "is-notice";
}

function activitySignalText(item, entry) {
  const seatChange = activitySeatChange(item, entry);
  if (seatChange) return `${seatChange.delta > 0 ? "+" : ""}${seatChange.delta}`;
  const labels = {
    new_screenings: "NEW",
    booking_opened: "OPEN",
    booking_closed: "CLOSE",
  };
  return labels[item.kind] || "CHANGE";
}

function changeFieldLabel(field) {
  const labels = {
    free_seats: "잔여석",
    total_seats: "전체 좌석",
    control_yn: "예매 상태",
    screening_date: "상영일",
    start_time: "시작 시각",
    end_time: "종료 시각",
    screen_name: "상영관",
    screen_no: "상영관 번호",
    sequence: "회차",
    format_name: "포맷",
    booking_url: "예매 링크",
    exists: "회차 등록",
  };
  return labels[field] || field || "변경";
}

function changeValue(field, value) {
  if (value === null || value === undefined || value === "") return "없음";
  if (field === "control_yn") {
    if (value === "Y") return "예매 준비중";
    if (value === "N") return "예매 가능";
  }
  if (field === "screening_date") return displayDate(String(value));
  if (field === "start_time" || field === "end_time") return displayTime(String(value));
  if (field === "free_seats" || field === "total_seats") return `${value}석`;
  return String(value);
}

function formattedChange(change) {
  const current = recordOrEmpty(change);
  const field = current.field || current.name || "";
  const before = current.before ?? current.previous ?? current.old_value;
  const after = current.after ?? current.current ?? current.new_value;
  if (field === "exists") {
    if (!before && after) return "신규 회차 등록";
    if (before && !after) return "회차 기록 종료";
  }
  if (field === "booking_url") {
    return `예매 링크 ${before ? "있음" : "없음"} → ${after ? "있음" : "없음"}`;
  }
  return `${changeFieldLabel(field)} ${changeValue(field, before)} → ${changeValue(field, after)}`;
}

function activityChangeText(item, entry = { screening: {}, previous: {}, changes: [] }) {
  const changes = Array.isArray(entry.changes)
    ? entry.changes.filter((change) => change && typeof change === "object")
    : [];
  if (changes.length) {
    const visible = changes.slice(0, 2).map(formattedChange);
    if (changes.length > visible.length) visible.push(`외 ${changes.length - visible.length}건`);
    return visible.join(" · ");
  }

  const screening = recordOrEmpty(entry.screening);
  const previous = recordOrEmpty(entry.previous);
  if (["seat_increases", "seat_decreases"].includes(item.kind)
      && previous.free_seats !== undefined) {
    return `잔여석 ${changeValue("free_seats", previous.free_seats)} → ${changeValue("free_seats", screening.free_seats)}`;
  }
  if (item.kind === "total_seats_changed" && previous.total_seats !== undefined) {
    return `전체 좌석 ${changeValue("total_seats", previous.total_seats)} → ${changeValue("total_seats", screening.total_seats)}`;
  }
  const labels = {
    new_screenings: "미등록 → 신규 회차",
    booking_opened: "예매 준비중 → 예매 가능",
    booking_closed: "예매 가능 → 예매 종료",
    screening_updated: "이전 정보 → 상영 정보 변경",
  };
  return labels[item.kind] || "변화 전후 정보 미확인";
}

function deliveryStatus(item) {
  const notification = recordOrEmpty(item.notification);
  const sentAt = notification.sent_at || item.sent_at;
  const deadAt = notification.dead_lettered_at || item.dead_lettered_at;
  const status = notification.status || item.status;
  if (sentAt) return "Telegram 발송 완료";
  if (deadAt) return "Telegram 발송 실패";
  if (Object.prototype.hasOwnProperty.call(item, "notification") && item.notification === null) return "기록만 저장";
  if (!item.notification && !status) return "알림 정보 없음";
  return status === "pending" ? "Telegram 발송 대기" : status || "상태 미확인";
}

function activityIsDead(item) {
  return Boolean(item.dead_lettered_at || item.notification?.dead_lettered_at);
}

function activityOverviewHtml(item) {
  const entries = activityEntries(item);
  const entry = entries[0] || { screening: {}, previous: {}, changes: [] };
  const screening = recordOrEmpty(entry.screening);
  const seatChange = activitySeatChange(item, entry);
  const additional = entries.length > 1 ? ` 외 ${entries.length - 1}개 회차` : "";
  const timestamp = activityTimestamp(item);
  const formatName = screening.format_name || "상영 포맷 미확인";
  const remaining = screening.free_seats === undefined
    ? "확인 불가"
    : `${Number(screening.free_seats).toLocaleString("ko-KR")} / ${Number(screening.total_seats || 0).toLocaleString("ko-KR")}석`;
  const changeText = seatChange
    ? `${seatChange.before.toLocaleString("ko-KR")} → ${seatChange.after.toLocaleString("ko-KR")}석`
    : activityChangeText(item, entry);
  return `
    <div class="activity-heading">
      <span class="activity-signal" aria-label="${escapeHtml(activityTitle(item))}">${escapeHtml(activitySignalText(item, entry))}</span>
      <div>
        <strong>${escapeHtml(activityTitle(item))}</strong>
        <small>${escapeHtml(screening.movie_name || "영화 정보 미확인")} · ${escapeHtml(formatName)}${escapeHtml(additional)}</small>
      </div>
      <time class="activity-time" datetime="${escapeHtml(timestamp)}">
        <span>${escapeHtml(humanDateTime(timestamp))}</span>
        <span class="activity-age" data-relative-time="${escapeHtml(timestamp)}">${escapeHtml(relativeTimeText(timestamp))}</span>
      </time>
    </div>
    <dl class="activity-facts">
      <div><dt>회차 / 일시</dt><dd>${escapeHtml(activitySessionText(screening))}</dd></div>
      <div><dt>포맷 / 상영관</dt><dd>${escapeHtml(formatName)} · ${escapeHtml(activityHallText(screening))}</dd></div>
      <div><dt>변동</dt><dd>${escapeHtml(changeText)}</dd></div>
      <div><dt>현재 잔여석</dt><dd>${escapeHtml(remaining)}</dd></div>
    </dl>
    <div class="activity-actions">
      <span>${escapeHtml(deliveryStatus(item))}</span>
      ${bookingButton(activityBookingUrl(item, screening))}
    </div>`;
}

function screeningDetailHtml(screening = {}, previousValue = null, changes = []) {
  const previous = typeof previousValue === "number"
    ? { free_seats: previousValue }
    : recordOrEmpty(previousValue);
  const changeText = Array.isArray(changes) && changes.length
    ? changes.map(formattedChange).join(" · ")
    : previous.free_seats !== undefined
      ? `잔여석 ${changeValue("free_seats", previous.free_seats)} → ${changeValue("free_seats", screening.free_seats)}`
      : null;
  const screeningDate = screening.screening_date
    ? `${displayDate(screening.screening_date)} (${screening.screening_date})`
    : "—";
  const sale = screening.control_yn === undefined
    ? "상태 미확인"
    : `${saleStatus(screening.control_yn, screening.free_seats)} (제어값 ${screening.control_yn})`;
  return `
    ${detailPairsHtml([
      ["영화", screening.movie_name],
      ["극장", screening.site_name],
      ["상영일", screeningDate],
      ["상영 시간", `${displayTime(screening.start_time)}–${displayTime(screening.end_time)}`],
      ["상영관", `${screening.screen_name || "—"} (번호 ${screening.screen_no || "—"})`],
      ["회차", screening.sequence],
      ["포맷", screening.format_name],
      ["예매 상태", sale],
      ["잔여 / 전체 좌석", `${screening.free_seats ?? "—"} / ${screening.total_seats ?? "—"}석`],
      ...(changeText ? [["변화 전후", changeText]] : []),
      ["회사 코드", screening.company_code],
      ["극장 코드", screening.site_no],
      ["영화 코드", screening.movie_no],
      ["상영관 등급 코드", screening.screen_grade_code],
    ])}
    ${screening.booking_url ? `<div class="detail-link-row">${bookingButton(screening.booking_url)}</div>` : ""}`;
}

function activityEntriesHtml(item) {
  const entries = activityEntries(item);
  if (entries.length) {
    return entries.map((entry, index) => {
      const screening = {
        ...recordOrEmpty(entry.screening),
        booking_url: activityBookingUrl(index === 0 ? item : {}, entry.screening),
      };
      return `
      <section class="activity-entry">
        <h4>상영 회차 ${index + 1}</h4>
        ${screeningDetailHtml(screening, entry.previous, entry.changes)}
      </section>`;
    }).join("");
  }
  return `<p class="detail-empty">${item.details_complete === false ? "이 기록은 요약 정보만 보존되어 있습니다." : "상영 상세 정보가 없습니다."}</p>`;
}

function activityDetailsHtml(item) {
  const key = String(item.id);
  const open = state.expandedActivities.has(key) ? " open" : "";
  const notification = recordOrEmpty(item.notification);
  const count = activityEntries(item).length;
  const completeness = item.details_complete === true
    ? "상세 보존"
    : item.details_complete === false ? "요약만 보존" : "상태 미확인";
  const eventMeta = detailPairsHtml([
    ["이벤트 ID", item.id],
    ["대상 ID", item.target_id],
    ["상영 ID", item.screening_id],
    ["리비전", item.revision],
    ["상영 키", item.screening_key || item.event_key],
    ["종류 코드", item.kind],
    ["감지 시각", humanDateTime(activityTimestamp(item))],
    ["상세 완전성", completeness],
    ["알림 상태", deliveryStatus(item)],
    ["Telegram 발송", (notification.sent_at || item.sent_at) ? humanDateTime(notification.sent_at || item.sent_at) : "아직 없음"],
    ["다음 시도", (notification.next_attempt_at || item.next_attempt_at) ? humanDateTime(notification.next_attempt_at || item.next_attempt_at) : "없음"],
    ["실패 확정", (notification.dead_lettered_at || item.dead_lettered_at) ? humanDateTime(notification.dead_lettered_at || item.dead_lettered_at) : "없음"],
    ["시도 횟수", notification.attempts ?? item.attempts ?? "—"],
    ["Telegram 분할 발송 수", notification.delivered_parts ?? item.delivered_parts ?? "—"],
    ["마지막 오류", notification.last_error || item.last_error || "없음"],
  ], "detail-pairs activity-event-meta");
  return `
    <details class="activity-details" data-activity-detail-id="${escapeHtml(key)}"${open}>
      <summary>이벤트 상세 <span>${count}개 항목 · ${escapeHtml(deliveryStatus(item))}</span></summary>
      <div class="activity-detail-content">
        ${eventMeta}
        ${item.details_complete === false ? '<p class="detail-notice">이전 버전의 상세 필드는 복원되지 않아 확인 가능한 값만 표시합니다.</p>' : ""}
        ${activityEntriesHtml(item)}
      </div>
    </details>`;
}

function renderActivity() {
  const target = selectedTarget();
  const alerts = state.recentAlerts
    .filter((item) => item.kind === "seat_increases"
      && item.notification !== null
      && (!target || Number(item.target_id) === Number(target.id)))
    .slice(0, 6);
  byId("activityCount").textContent = alerts.length;
  const signature = JSON.stringify([
    target?.id ?? null,
    alerts.map((item) => [
      item.id,
      item.revision,
      item.kind,
      item.booking_url,
      item.screening?.free_seats,
      item.notification?.id,
      item.notification?.status,
      item.notification?.sent_at,
      item.notification?.dead_lettered_at,
    ]),
  ]);
  if (signature === state.recentAlertsRenderSignature) return;
  state.recentAlertsRenderSignature = signature;
  if (!alerts.length) {
    byId("activityList").innerHTML = '<p class="activity-empty">설정 기준을 넘은 최근 잔여석 증가가 없습니다.</p>';
    return;
  }
  byId("activityList").innerHTML = alerts.map((item) => `
    <article class="activity-summary-item ${activityToneClass(item)} ${activityIsDead(item) ? "is-dead" : ""}">
      ${activityOverviewHtml(item)}
    </article>`).join("");
}

function renderActivityTargetFilter() {
  const select = byId("activityTargetFilter");
  const selected = select.value || state.activityLog.filters.targetId;
  select.innerHTML = '<option value="">전체 대상</option>' + state.targets.map((target) => (
    `<option value="${escapeHtml(target.id)}">${escapeHtml(target.site_name)} · ${escapeHtml(target.movie_name)} · ${escapeHtml(targetFormatLabel(target))}</option>`
  )).join("");
  select.value = [...select.options].some((option) => option.value === String(selected))
    ? String(selected)
    : "";
}

function activityFilterDescription() {
  const filters = state.activityLog.filters;
  const parts = [];
  if (filters.targetId) {
    const target = state.targets.find((item) => String(item.id) === String(filters.targetId));
    parts.push(target ? `${target.site_name} · ${target.movie_name} · ${targetFormatLabel(target)}` : `대상 ${filters.targetId}`);
  }
  if (filters.kind) parts.push(activityTitle({ kind: filters.kind }));
  if (filters.screeningId) parts.push(`상영 ID ${filters.screeningId}`);
  return parts.length ? parts.join(" · ") : "전체 조건";
}

function activityFiltersKey(filters = state.activityLog.filters) {
  return JSON.stringify([
    String(filters.targetId || ""),
    String(filters.screeningId || ""),
    String(filters.kind || ""),
  ]);
}

function activityQueryParams({ limit = 20, cursor = null } = {}) {
  const filters = state.activityLog.filters;
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor !== null && cursor !== undefined && cursor !== "") params.set("cursor", String(cursor));
  if (filters.targetId) params.set("target_id", String(filters.targetId));
  if (filters.screeningId) params.set("screening_id", String(filters.screeningId));
  if (filters.kind) params.set("kind", filters.kind);
  return params;
}

function activityHeadId(items) {
  if (!Array.isArray(items) || !items.length || items[0]?.id === undefined || items[0]?.id === null) return null;
  return String(items[0].id);
}

function clearPendingActivityRecords() {
  state.liveSync.pendingHeadId = null;
  state.liveSync.pendingNewCount = 0;
  const notice = byId("activityNewRecords");
  if (notice) notice.hidden = true;
}

function showPendingActivityRecords(items) {
  const previousHeadId = state.liveSync.activityHeadId;
  const newHeadId = activityHeadId(items);
  if (newHeadId === null || newHeadId === previousHeadId) return;
  const previousIndex = previousHeadId === null
    ? -1
    : items.findIndex((item) => String(item.id) === previousHeadId);
  state.liveSync.pendingHeadId = newHeadId;
  state.liveSync.pendingNewCount = previousIndex > 0 ? previousIndex : previousIndex === 0 ? 0 : 20;
  const notice = byId("activityNewRecords");
  const text = byId("activityNewRecordsText");
  if (text) {
    text.textContent = previousIndex > 0
      ? `새 감지 기록 ${previousIndex}건이 있습니다.`
      : previousHeadId === null
        ? "새 감지 기록이 있습니다."
        : "새 감지 기록이 20건 이상 있습니다.";
  }
  if (notice) notice.hidden = false;
}

function activityCountdownTarget() {
  const filteredTargetId = state.activityLog.filters.targetId;
  if (filteredTargetId) {
    return state.targets.find((target) => String(target.id) === String(filteredTargetId)) || null;
  }
  const enabledTargets = state.targets.filter((target) => target.enabled);
  if (!enabledTargets.length) return null;
  const phasePriority = { running: 0, queued: 1, idle: 2, disabled: 3 };
  return [...enabledTargets].sort((left, right) => {
    const phaseDifference = phasePriority[refreshPhase(left)] - phasePriority[refreshPhase(right)];
    if (phaseDifference !== 0) return phaseDifference;
    const leftAt = timestampMs(left.next_poll_at) ?? Number.POSITIVE_INFINITY;
    const rightAt = timestampMs(right.next_poll_at) ?? Number.POSITIVE_INFINITY;
    return leftAt - rightAt;
  })[0];
}

function activityPollCountdownState(now = Date.now() + state.serverClockOffsetMs) {
  const target = activityCountdownTarget();
  if (!target) {
    const workerError = state.workerOk === false;
    return {
      value: "—",
      label: workerError ? "감지 확인 필요" : "활성 감시 대상 없음",
      ok: false,
      error: workerError,
    };
  }
  const context = `${target.movie_name} · ${targetFormatLabel(target)}`;
  const phase = refreshPhase(target);
  const targetError = Boolean(target.last_error);
  const workerError = state.workerOk === false;
  const error = targetError || workerError;
  const healthLabel = workerError
    ? "감지 확인 필요"
    : targetError
      ? "조회 오류"
      : state.workerOk === null
        ? "상태 확인 중"
        : phase === "disabled" ? "감지 중지" : "감지 정상";
  const label = `${context} · ${healthLabel}`;
  const ok = state.workerOk === true && !targetError && phase !== "disabled";
  if (phase === "disabled") return { value: "중지", label, ok: false, error };
  if (phase === "running") return { value: "조회 중", label, ok, error };
  if (phase === "queued") return { value: "대기 중", label, ok, error };
  const nextPollAt = timestampMs(target.next_poll_at);
  if (nextPollAt === null) return { value: "계산 중", label, ok, error };
  const remainingSeconds = Math.max(0, Math.ceil((nextPollAt - now) / 1000));
  return {
    value: remainingSeconds > 0 ? `${remainingSeconds}초` : "대기 중",
    label,
    ok,
    error,
  };
}

function updateActivityPollCountdown(now = Date.now() + state.serverClockOffsetMs) {
  if (!activityPath) return;
  const countdown = activityPollCountdownState(now);
  const dedicated = byId("activityPollCountdown");
  if (dedicated) {
    dedicated.textContent = countdown.value;
    const workerLabel = byId("activityWorkerLabel");
    if (workerLabel && workerLabel.textContent !== countdown.label) {
      workerLabel.textContent = countdown.label;
    }
    const status = byId("activityPollStatus");
    status?.classList.toggle("is-ok", countdown.ok);
    status?.classList.toggle("is-error", countdown.error);
    return;
  }
  const result = byId("activityResultText");
  if (!result) return;
  const base = result.dataset.baseText || result.textContent;
  result.dataset.baseText = base;
  result.textContent = `${base} · ${countdown.label} · 다음 조회 ${countdown.value}`;
}

const activityFocusableSelector = "summary, a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])";

function activityListFocusMarker() {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return null;
  const article = active.closest("[data-activity-id]");
  if (!article) return null;
  const focusable = [...article.querySelectorAll(activityFocusableSelector)];
  const index = focusable.indexOf(active);
  if (index < 0) return null;
  return { activityId: article.dataset.activityId, index };
}

function restoreActivityListFocus(marker) {
  if (!marker) return;
  const article = [...document.querySelectorAll("[data-activity-id]")]
    .find((item) => item.dataset.activityId === marker.activityId);
  const target = article?.querySelectorAll(activityFocusableSelector)[marker.index];
  if (target instanceof HTMLElement) target.focus({ preventScroll: true });
}

function announceActivityLiveUpdate(message) {
  const status = byId("activityLiveUpdateStatus");
  if (!status) return;
  if (status.textContent !== message) {
    status.textContent = message;
    return;
  }
  status.textContent = "";
  requestAnimationFrame(() => { status.textContent = message; });
}

function renderActivityPage() {
  const log = state.activityLog;
  const list = byId("activityFullList");
  list.setAttribute("aria-busy", String(log.loading));
  if (!log.items.length && !log.loading) {
    list.innerHTML = '<p class="activity-empty">조건에 맞는 감지 기록이 없습니다.</p>';
  } else if (log.items.length) {
    list.innerHTML = log.items.map((item) => `
      <article class="activity-full-item ${activityToneClass(item)} ${activityIsDead(item) ? "is-dead" : ""}" data-activity-id="${escapeHtml(item.id)}">
        <header>${activityOverviewHtml(item)}</header>
        ${activityDetailsHtml(item)}
      </article>`).join("");
  }
  const resultText = `${activityFilterDescription()} · ${log.page}페이지 · ${log.items.length}건`;
  byId("activityResultText").dataset.baseText = resultText;
  byId("activityResultText").textContent = resultText;
  byId("activityPageStatus").textContent = `${log.page}페이지`;
  byId("prevActivityPage").disabled = log.loading || log.page <= 1;
  byId("nextActivityPage").disabled = log.loading || !log.hasMore || log.nextCursor === null;
  byId("applyActivityFilters").disabled = log.loading;
  byId("resetActivityFilters").disabled = log.loading;
  updateActivityPollCountdown();
}

async function loadActivityPage(cursor = null, { page = 1, reset = false } = {}) {
  const log = state.activityLog;
  const requestId = log.requestId + 1;
  const filterKey = activityFiltersKey();
  log.requestId = requestId;
  log.loading = true;
  if (reset) {
    log.pageCursors = [null];
    clearPendingActivityRecords();
  }
  renderActivityPage();

  const params = activityQueryParams({ limit: 20, cursor });

  try {
    const data = await api(`/api/v1/activity?${params.toString()}`);
    if (requestId !== log.requestId || filterKey !== activityFiltersKey()) return;
    const items = Array.isArray(data.items)
      ? data.items
      : Array.isArray(data.activity) ? data.activity : [];
    log.items = items;
    log.page = page;
    log.nextCursor = data.next_cursor ?? null;
    log.hasMore = typeof data.has_more === "boolean"
      ? data.has_more
      : log.nextCursor !== null;
    if (page === 1) {
      state.liveSync.activityFilterKey = filterKey;
      state.liveSync.activityHeadId = activityHeadId(items);
      clearPendingActivityRecords();
    }
  } finally {
    if (requestId === log.requestId) {
      log.loading = false;
      renderActivityPage();
    }
  }
}

async function syncFullActivityLive() {
  const log = state.activityLog;
  if (log.loading) return;
  const filterKey = activityFiltersKey();
  const page = log.page;
  const requestId = log.requestId;
  const params = activityQueryParams({ limit: 20 });
  const data = await api(`/api/v1/activity?${params.toString()}`);
  if (log.loading
      || requestId !== log.requestId
      || page !== log.page
      || filterKey !== activityFiltersKey()) return;

  const items = Array.isArray(data.items)
    ? data.items
    : Array.isArray(data.activity) ? data.activity : [];
  if (page !== 1) {
    if (state.liveSync.activityFilterKey === filterKey) showPendingActivityRecords(items);
    return;
  }

  const nextCursor = data.next_cursor ?? null;
  const hasMore = typeof data.has_more === "boolean"
    ? data.has_more
    : nextCursor !== null;
  const contentChanged = JSON.stringify(log.items) !== JSON.stringify(items)
    || log.nextCursor !== nextCursor
    || log.hasMore !== hasMore;
  const previousHeadId = activityHeadId(log.items);
  const nextHeadId = activityHeadId(items);
  const focusMarker = contentChanged ? activityListFocusMarker() : null;
  log.items = items;
  log.nextCursor = nextCursor;
  log.hasMore = hasMore;
  state.liveSync.activityFilterKey = filterKey;
  state.liveSync.activityHeadId = activityHeadId(items);
  clearPendingActivityRecords();
  if (contentChanged) {
    if (nextHeadId !== null && nextHeadId !== previousHeadId) {
      const previousIndex = previousHeadId === null
        ? -1
        : items.findIndex((item) => String(item.id) === previousHeadId);
      const count = previousIndex > 0 ? previousIndex : 1;
      announceActivityLiveUpdate(`새 감지 기록 ${count}건을 반영했습니다.`);
    }
    renderActivityPage();
    restoreActivityListFocus(focusMarker);
  }
}

async function showLatestActivity() {
  await loadActivityPage(null, { page: 1, reset: true });
  byId("activityViewTitle")?.focus({ preventScroll: true });
}

function reportBackgroundError(error) {
  console.warn("MovieMax background sync failed", error);
}

async function loadActivityStatus() {
  const requestId = state.liveSync.statusRequestId + 1;
  state.liveSync.statusRequestId = requestId;
  const requestStartedAt = Date.now();
  const data = await api("/api/v1/bootstrap");
  if (requestId !== state.liveSync.statusRequestId) return;
  const serverTime = timestampMs(data.server_time);
  if (serverTime !== null) {
    state.serverClockOffsetMs = serverTime - ((requestStartedAt + Date.now()) / 2);
  }
  state.targets = Array.isArray(data.targets) ? data.targets : [];
  renderWorker(data.status || {});
  renderActivityTargetFilter();
  updateActivityPollCountdown();
}

async function syncLiveView({ force = false } = {}) {
  if ((!force && document.hidden) || state.liveSync.inFlight) return;
  const workspace = document.querySelector(".workspace");
  if (workspace?.getAttribute("aria-busy") === "true") return;
  state.liveSync.inFlight = true;
  try {
    if (activityPath) {
      await Promise.all([loadActivityStatus(), syncFullActivityLive()]);
      return;
    }
    const targetId = state.selectedTargetId;
    if (targetId) await loadRecentAlerts(targetId);
  } finally {
    state.liveSync.inFlight = false;
  }
}

async function applyActivityFilters(event) {
  event.preventDefault();
  const form = byId("activityFilterForm");
  if (!form.reportValidity()) return;
  state.activityLog.filters = {
    targetId: byId("activityTargetFilter").value,
    screeningId: byId("activityScreeningFilter").value.trim(),
    kind: byId("activityKindFilter").value,
  };
  await loadActivityPage(null, { page: 1, reset: true });
}

async function resetActivityFilters() {
  byId("activityTargetFilter").value = "";
  byId("activityKindFilter").value = "";
  byId("activityScreeningFilter").value = "";
  state.activityLog.filters = { targetId: "", screeningId: "", kind: "" };
  await loadActivityPage(null, { page: 1, reset: true });
}

async function changeActivityPage(direction) {
  const log = state.activityLog;
  if (log.loading) return;
  if (direction > 0) {
    if (!log.hasMore || log.nextCursor === null) return;
    const nextPage = log.page + 1;
    log.pageCursors[nextPage - 1] = log.nextCursor;
    await loadActivityPage(log.nextCursor, { page: nextPage });
    return;
  }
  if (log.page <= 1) return;
  const previousPage = log.page - 1;
  const previousCursor = log.pageCursors[previousPage - 1] ?? null;
  await loadActivityPage(previousCursor, { page: previousPage });
}

function initializePathView() {
  byId("overviewView").classList.toggle("is-hidden", activityPath);
  byId("activityView").classList.toggle("is-hidden", !activityPath);
  if (activityPath) document.title = "전체 감지 기록 · max.wondering";
}

function renderTelegram() {
  const configured = Boolean(state.telegram.configured);
  const enabled = configured ? Boolean(state.telegram.enabled) : true;
  const active = configured && enabled;
  byId("telegramMiniTitle").textContent = active
    ? `@${state.telegram.bot_username || "연결된 봇"}`
    : configured ? "Telegram 일시 중지" : "Telegram 연결 안 됨";
  byId("telegramMiniText").textContent = configured
    ? state.telegram.chat_id_masked || "Chat ID 설정 필요"
    : "봇을 연결하면 휴대폰으로 알립니다.";
  byId("tokenStatus").textContent = configured ? "· 저장됨" : "· 미설정";
  byId("botToken").required = !configured;
  byId("chatId").value = state.telegram.chat_id || "";
  byId("telegramEnabled").checked = enabled;
  byId("testTelegram").disabled = !active;
  byId("testTelegram").title = active
    ? "현재 서버에 저장된 Telegram 설정으로 전송합니다"
    : "Telegram 설정을 저장하고 활성화한 뒤 사용할 수 있습니다";
}

const webPushDisabledStorageKey = "moviemax.webPush.disabled";
const webPushPendingEndpointStorageKey = "moviemax.webPush.pendingEndpoint";

function readWebPushStorage(key) {
  try {
    return window.localStorage.getItem(key) || "";
  } catch (_error) {
    return "";
  }
}

function writeWebPushStorage(key, value) {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch (_error) { /* Storage can be unavailable in a private context. */ }
}

function setWebPushSyncSuppressed(suppressed) {
  state.webPush.syncSuppressed = Boolean(suppressed);
  writeWebPushStorage(webPushDisabledStorageKey, suppressed ? "1" : "");
}

function rememberWebPushPendingEndpoint(endpoint) {
  state.webPush.pendingEndpoint = String(endpoint || "");
  writeWebPushStorage(webPushPendingEndpointStorageKey, state.webPush.pendingEndpoint);
}

function webPushFeedback(message = "", kind = "info") {
  const feedback = byId("webPushFeedback");
  feedback.hidden = !message;
  feedback.textContent = message;
  feedback.className = `form-feedback is-${kind}`;
  feedback.setAttribute("role", kind === "error" ? "alert" : "status");
}

function supportsWebPush() {
  return window.isSecureContext
    && "serviceWorker" in navigator
    && "PushManager" in window
    && "Notification" in window;
}

function urlBase64ToUint8Array(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const raw = window.atob((value + padding).replaceAll("-", "+").replaceAll("_", "/"));
  return Uint8Array.from(raw, (character) => character.charCodeAt(0));
}

function webPushSubscriptionBody(subscription) {
  const value = subscription.toJSON();
  if (!value.endpoint || !value.keys?.p256dh || !value.keys?.auth) {
    throw new Error("브라우저가 유효한 Web Push 구독 정보를 제공하지 않았습니다.");
  }
  return {
    endpoint: value.endpoint,
    expiration_time: value.expirationTime ?? null,
    keys: {
      p256dh: value.keys.p256dh,
      auth: value.keys.auth,
    },
  };
}

function applyWebPushSubscriptionResponse(result) {
  Object.assign(state.webPush, recordOrEmpty(result?.web_push));
  const subscriptionStatus = recordOrEmpty(result?.subscription);
  const requiresResubscribe = subscriptionStatus.requires_resubscribe === true;
  const active = subscriptionStatus.active === true && !requiresResubscribe;
  state.webPush.serverSynced = active;
  state.webPush.serverInactive = subscriptionStatus.active === false || requiresResubscribe;
  return { active, requiresResubscribe };
}

async function webPushRegistration() {
  if (!supportsWebPush()) return null;
  if (state.webPush.registration) return state.webPush.registration;
  const registration = await navigator.serviceWorker.register("/service-worker.js", {
    scope: "/",
    updateViaCache: "none",
  });
  state.webPush.registration = registration;
  return registration;
}

async function createWebPushSubscription(registration) {
  const publicKey = String(state.webPush.public_key || "");
  if (!publicKey) throw new Error("서버의 Web Push 공개 키를 불러오지 못했습니다.");
  return registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(publicKey),
  });
}

async function replaceInactiveWebPushSubscription(registration, subscription) {
  const endpoint = String(subscription?.endpoint || "");
  if (!endpoint) throw new Error("갱신할 브라우저 구독 주소를 찾지 못했습니다.");
  rememberWebPushPendingEndpoint(endpoint);
  const deleted = await api("/api/v1/web-push/subscription", {
    method: "DELETE",
    body: { endpoint },
  });
  Object.assign(state.webPush, recordOrEmpty(deleted.web_push));
  state.webPush.serverSynced = false;
  state.webPush.serverInactive = false;
  rememberWebPushPendingEndpoint("");
  let unsubscribed = false;
  try {
    unsubscribed = await subscription.unsubscribe();
  } catch (error) {
    setWebPushSyncSuppressed(true);
    rememberWebPushPendingEndpoint(endpoint);
    state.webPush.serverInactive = true;
    throw error;
  }
  if (!unsubscribed) {
    setWebPushSyncSuppressed(true);
    rememberWebPushPendingEndpoint(endpoint);
    state.webPush.serverInactive = true;
    throw new Error("기존 브라우저 구독을 정리하지 못했습니다. 알림 끄기를 누른 뒤 다시 시도해 주세요.");
  }
  state.webPush.subscription = null;
  const replacement = await createWebPushSubscription(registration);
  state.webPush.subscription = replacement;
  return replacement;
}

function renderWebPush() {
  const supported = state.webPush.supported;
  const localSubscribed = Boolean(state.webPush.subscription);
  const active = localSubscribed
    && state.webPush.serverSynced
    && !state.webPush.syncSuppressed;
  const cleanupPending = state.webPush.syncSuppressed
    && (localSubscribed || Boolean(state.webPush.pendingEndpoint) || state.webPush.serverSynced);
  const permission = "Notification" in window ? Notification.permission : "unsupported";
  const serverCount = Number(state.webPush.subscription_count) || 0;
  const miniTitle = byId("webPushMiniTitle");
  const miniText = byId("webPushMiniText");
  const statusText = byId("webPushDeviceStatus");
  const helpText = byId("webPushDeviceHelp");

  if (supported === null) {
    miniTitle.textContent = "브라우저 알림 확인 중";
    miniText.textContent = "이 기기의 지원 상태를 확인합니다.";
    statusText.textContent = "지원 상태를 확인하는 중입니다.";
    helpText.textContent = "알림 권한은 켜기 버튼을 누를 때만 요청합니다.";
  } else if (!supported) {
    miniTitle.textContent = "브라우저 알림 사용 불가";
    miniText.textContent = "지원되는 HTTPS 웹 앱에서 설정하세요.";
    statusText.textContent = "현재 실행 환경에서는 Web Push를 사용할 수 없습니다.";
    helpText.textContent = "iPhone·iPad에서는 홈 화면에 추가한 웹 앱으로 다시 열어 설정하세요.";
  } else if (cleanupPending) {
    miniTitle.textContent = "브라우저 알림 해제 필요";
    miniText.textContent = "서버 또는 이 기기의 구독 정리를 다시 시도하세요.";
    statusText.textContent = "브라우저 알림 해제가 아직 완전히 끝나지 않았습니다.";
    helpText.textContent = "알림 끄기를 다시 누르면 남은 서버·브라우저 구독만 정리합니다.";
  } else if (permission === "denied") {
    miniTitle.textContent = "브라우저 알림 차단됨";
    miniText.textContent = "브라우저 또는 기기 설정에서 권한을 허용하세요.";
    statusText.textContent = "알림 권한이 차단되어 있습니다.";
    helpText.textContent = "브라우저의 사이트 설정에서 알림 권한을 허용한 뒤 다시 시도하세요.";
  } else if (active) {
    miniTitle.textContent = "브라우저 알림 켜짐";
    miniText.textContent = `이 기기 연결됨 · 서버 구독 ${serverCount}개`;
    statusText.textContent = "이 기기의 브라우저 알림이 켜져 있습니다.";
    helpText.textContent = "기준을 충족한 잔여석 증가를 사이트가 닫혀 있어도 알립니다.";
  } else if (localSubscribed && state.webPush.serverInactive) {
    miniTitle.textContent = "브라우저 구독 갱신 필요";
    miniText.textContent = "만료되거나 중지된 구독을 새로 연결하세요.";
    statusText.textContent = "서버가 이 브라우저 구독을 비활성 상태로 확인했습니다.";
    helpText.textContent = "알림 켜기를 누르면 기존 구독을 정리하고 새 구독을 만듭니다.";
  } else if (localSubscribed) {
    miniTitle.textContent = "브라우저 알림 서버 연결 필요";
    miniText.textContent = "이 기기의 구독을 서버에 다시 저장하세요.";
    statusText.textContent = "브라우저 구독은 있지만 서버 연결이 완료되지 않았습니다.";
    helpText.textContent = "알림 켜기를 누르면 기존 구독을 서버에 다시 저장합니다.";
  } else {
    miniTitle.textContent = "브라우저 알림 꺼짐";
    miniText.textContent = serverCount ? `다른 기기 구독 ${serverCount}개` : "이 기기에 알림을 연결할 수 있습니다.";
    statusText.textContent = "이 기기는 아직 브라우저 알림에 연결되지 않았습니다.";
    helpText.textContent = "켜기 버튼을 누르면 브라우저가 알림 권한을 요청합니다.";
  }

  const busy = Boolean(state.webPush.loading);
  const missingConfiguration = !state.webPush.configured
    || (!localSubscribed && !state.webPush.public_key);
  byId("enableWebPush").disabled = busy
    || !supported
    || active
    || cleanupPending
    || missingConfiguration
    || permission === "denied";
  byId("disableWebPush").disabled = busy
    || (!localSubscribed && !state.webPush.pendingEndpoint && !state.webPush.serverSynced);
  byId("testWebPush").disabled = busy || !active;
  byId("webPushMiniAction").disabled = busy;
}

async function removeWebPushSubscription(subscription, endpointValue = "") {
  const endpoint = String(endpointValue || subscription?.endpoint || state.webPush.pendingEndpoint || "");
  if (endpoint) rememberWebPushPendingEndpoint(endpoint);
  const serverAttempt = endpoint
    ? Promise.resolve().then(() => api("/api/v1/web-push/subscription", {
      method: "DELETE",
      body: { endpoint },
    }))
    : Promise.resolve(null);
  const browserAttempt = subscription
    ? Promise.resolve().then(() => subscription.unsubscribe())
    : Promise.resolve(true);
  const [serverResult, browserResult] = await Promise.allSettled([
    serverAttempt,
    browserAttempt,
  ]);

  let serverRemoved = !endpoint;
  let browserRemoved = !subscription;
  const errors = [];
  if (serverResult.status === "fulfilled") {
    serverRemoved = true;
    state.webPush.serverSynced = false;
    state.webPush.serverInactive = false;
    Object.assign(state.webPush, recordOrEmpty(serverResult.value?.web_push));
    if (state.webPush.pendingEndpoint === endpoint) rememberWebPushPendingEndpoint("");
  } else {
    errors.push(serverResult.reason);
  }
  if (browserResult.status === "fulfilled" && browserResult.value === true) {
    browserRemoved = true;
    if (!subscription
        || state.webPush.subscription === subscription
        || state.webPush.subscription?.endpoint === endpoint) {
      state.webPush.subscription = null;
    }
  } else if (browserResult.status === "rejected") {
    errors.push(browserResult.reason);
  } else {
    errors.push(new Error("브라우저가 이 기기의 알림 구독 해제를 완료하지 못했습니다."));
  }
  return { serverRemoved, browserRemoved, errors };
}

async function initializeWebPush({ syncServer = true } = {}) {
  state.webPush.supported = supportsWebPush();
  state.webPush.syncSuppressed = state.webPush.syncSuppressed
    || readWebPushStorage(webPushDisabledStorageKey) === "1";
  state.webPush.pendingEndpoint = state.webPush.pendingEndpoint
    || readWebPushStorage(webPushPendingEndpointStorageKey);
  renderWebPush();
  if (!state.webPush.supported) return;
  let subscription = null;
  let attemptedGeneration = null;
  try {
    const registration = await webPushRegistration();
    subscription = await registration.pushManager.getSubscription();
    state.webPush.subscription = subscription;
    if (state.webPush.syncSuppressed) {
      state.webPush.serverSynced = false;
      if (subscription || state.webPush.pendingEndpoint) {
        const cleanup = await removeWebPushSubscription(subscription);
        cleanup.errors.forEach(reportBackgroundError);
        if (cleanup.errors.length && byId("webPushDialog").open) {
          webPushFeedback("이 기기의 알림 해제를 완전히 마치지 못했습니다. 알림 끄기를 다시 눌러 주세요.", "error");
        }
      }
      return;
    }
    if (!subscription) {
      state.webPush.serverSynced = false;
      state.webPush.serverInactive = false;
      return;
    }
    if (!syncServer || Notification.permission !== "granted") {
      state.webPush.serverSynced = false;
      return;
    }
    if (state.webPush.loading) return;
    const generation = state.webPush.syncGeneration;
    attemptedGeneration = generation;
    const result = await api("/api/v1/web-push/subscription", {
      method: "PUT",
      body: webPushSubscriptionBody(subscription),
    });
    if (state.webPush.syncSuppressed) {
      rememberWebPushPendingEndpoint(subscription.endpoint);
      const cleanup = await removeWebPushSubscription(subscription);
      cleanup.errors.forEach(reportBackgroundError);
      return;
    }
    if (generation !== state.webPush.syncGeneration) return;
    const serverState = applyWebPushSubscriptionResponse(result);
    if (!serverState.active && byId("webPushDialog").open) {
      webPushFeedback("서버가 이 구독을 비활성 상태로 확인했습니다. 알림 켜기를 눌러 새 구독으로 갱신하세요.", "error");
    }
  } catch (error) {
    const superseded = attemptedGeneration !== null
      && attemptedGeneration !== state.webPush.syncGeneration
      && !state.webPush.syncSuppressed;
    if (!superseded) state.webPush.serverSynced = false;
    if (!superseded && !state.webPush.syncSuppressed && byId("webPushDialog").open) {
      webPushFeedback(`서버에 브라우저 구독을 저장하지 못했습니다. ${errorMessage(error)}`, "error");
    }
    reportBackgroundError(error);
  } finally {
    renderWebPush();
  }
}

function openWebPushDialog() {
  webPushFeedback();
  renderWebPush();
  const dialog = byId("webPushDialog");
  const initialFocus = [byId("enableWebPush"), byId("testWebPush"), byId("disableWebPush")]
    .find((button) => !button.disabled) || dialog.querySelector(".dialog-close");
  showDialog(dialog, initialFocus);
  initializeWebPush().catch(reportBackgroundError);
}

async function enableWebPush() {
  if (!supportsWebPush()) return;
  const button = byId("enableWebPush");
  const generation = state.webPush.syncGeneration + 1;
  state.webPush.syncGeneration = generation;
  setWebPushSyncSuppressed(false);
  state.webPush.loading = true;
  setButtonBusy(button, true);
  webPushFeedback("이 기기의 알림 권한과 구독을 설정하고 있습니다.", "pending");
  renderWebPush();
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      throw new Error(permission === "denied"
        ? "알림 권한이 차단되었습니다. 브라우저 사이트 설정에서 허용하세요."
        : "알림 권한이 허용되지 않았습니다.");
    }
    const registration = await webPushRegistration();
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      subscription = await createWebPushSubscription(registration);
    } else if (state.webPush.serverInactive) {
      subscription = await replaceInactiveWebPushSubscription(registration, subscription);
    }
    state.webPush.subscription = subscription;
    state.webPush.serverSynced = false;
    let result = await api("/api/v1/web-push/subscription", {
      method: "PUT",
      body: webPushSubscriptionBody(subscription),
    });
    if (state.webPush.syncSuppressed) {
      rememberWebPushPendingEndpoint(subscription.endpoint);
      const cleanup = await removeWebPushSubscription(subscription);
      cleanup.errors.forEach(reportBackgroundError);
      return;
    }
    if (generation !== state.webPush.syncGeneration) return;
    let serverState = applyWebPushSubscriptionResponse(result);
    if (!serverState.active && (state.webPush.serverInactive || serverState.requiresResubscribe)) {
      subscription = await replaceInactiveWebPushSubscription(registration, subscription);
      state.webPush.subscription = subscription;
      result = await api("/api/v1/web-push/subscription", {
        method: "PUT",
        body: webPushSubscriptionBody(subscription),
      });
      serverState = applyWebPushSubscriptionResponse(result);
    }
    if (!serverState.active) {
      throw new Error("서버가 브라우저 구독을 활성 상태로 저장하지 못했습니다. 다시 시도해 주세요.");
    }
    rememberWebPushPendingEndpoint("");
    webPushFeedback("이 기기의 브라우저 알림을 켰습니다. 시험 알림으로 수신을 확인할 수 있습니다.", "success");
    showToast("이 기기의 브라우저 알림을 켰습니다.");
  } catch (error) {
    state.webPush.serverSynced = false;
    webPushFeedback(errorMessage(error), "error");
  } finally {
    state.webPush.loading = false;
    setButtonBusy(button, false);
    renderWebPush();
  }
}

async function disableWebPush() {
  const subscription = state.webPush.subscription;
  const endpoint = String(subscription?.endpoint || state.webPush.pendingEndpoint || "");
  if (!subscription && !endpoint) return;
  const button = byId("disableWebPush");
  state.webPush.syncGeneration += 1;
  setWebPushSyncSuppressed(true);
  if (endpoint) rememberWebPushPendingEndpoint(endpoint);
  state.webPush.loading = true;
  setButtonBusy(button, true);
  webPushFeedback("서버 구독과 이 기기의 알림을 해제하고 있습니다.", "pending");
  renderWebPush();
  try {
    const cleanup = await removeWebPushSubscription(subscription, endpoint);
    if (cleanup.serverRemoved && cleanup.browserRemoved) {
      webPushFeedback("이 기기의 브라우저 알림을 껐습니다.", "success");
      showToast("이 기기의 브라우저 알림을 껐습니다.");
    } else {
      const details = cleanup.errors.map(errorMessage).join(" · ");
      webPushFeedback(`알림 해제를 완전히 마치지 못했습니다.${details ? ` ${details}` : ""} 다시 시도해 주세요.`, "error");
    }
  } finally {
    state.webPush.loading = false;
    setButtonBusy(button, false);
    renderWebPush();
  }
}

async function testWebPush() {
  const subscription = state.webPush.subscription;
  if (!subscription || !state.webPush.serverSynced) return;
  const button = byId("testWebPush");
  state.webPush.loading = true;
  setButtonBusy(button, true);
  webPushFeedback("서버에서 이 기기로 시험 알림을 보내고 있습니다.", "pending");
  renderWebPush();
  try {
    await api("/api/v1/web-push/test", {
      method: "POST",
      body: { endpoint: subscription.endpoint },
    });
    webPushFeedback("시험 알림을 발송했습니다. 운영체제 알림 영역에서 확인하세요.", "success");
  } catch (error) {
    if ([404, 410].includes(Number(error?.status))) {
      state.webPush.syncGeneration += 1;
      state.webPush.serverSynced = false;
      setWebPushSyncSuppressed(true);
      rememberWebPushPendingEndpoint(subscription.endpoint);
      const cleanup = await removeWebPushSubscription(subscription);
      cleanup.errors.forEach(reportBackgroundError);
      webPushFeedback(
        cleanup.errors.length
          ? "만료된 구독을 완전히 정리하지 못했습니다. 알림 끄기를 다시 눌러 주세요."
          : "브라우저 구독이 만료되어 정리했습니다. 이 기기 알림을 다시 켜 주세요.",
        "error",
      );
    } else {
      webPushFeedback(errorMessage(error), "error");
    }
  } finally {
    state.webPush.loading = false;
    setButtonBusy(button, false);
    renderWebPush();
  }
}

async function loadBootstrap({ preserveSelection = true } = {}) {
  const requestId = state.dashboardRequests.bootstrap + 1;
  state.dashboardRequests.bootstrap = requestId;
  const workspace = document.querySelector(".workspace");
  workspace?.setAttribute("aria-busy", "true");
  const requestStartedAt = Date.now();
  try {
    let data;
    try {
      data = await api("/api/v1/bootstrap");
    } catch (error) {
      if (requestId !== state.dashboardRequests.bootstrap) return;
      throw error;
    }
    if (requestId !== state.dashboardRequests.bootstrap) return;
    const serverTime = timestampMs(data.server_time);
    if (serverTime !== null) {
      state.serverClockOffsetMs = serverTime - ((requestStartedAt + Date.now()) / 2);
    }
    state.targets = Array.isArray(data.targets) ? data.targets : [];
    state.activity = Array.isArray(data.activity)
      ? data.activity
      : Array.isArray(data.activity?.items) ? data.activity.items : [];
    state.telegram = recordOrEmpty(data.telegram);
    Object.assign(state.webPush, recordOrEmpty(data.web_push));
    reconcileRefreshRequest();
    if (!preserveSelection || !state.targets.some((item) => item.id === state.selectedTargetId)) {
      state.selectedTargetId = state.targets[0]?.id || null;
    }
    const marker = focusMarker();
    renderWorker(data.status || {});
    renderTargets();
    renderActivityTargetFilter();
    restoreFocusIfLost(marker);
    renderTelegram();
    renderWebPush();
    if (state.selectedTargetId && !activityPath) {
      await Promise.all([
        loadScreenings(state.selectedTargetId),
        loadRecentAlerts(state.selectedTargetId),
      ]);
    } else {
      state.dashboardRequests.screenings += 1;
      state.dashboardRequests.alerts += 1;
      state.recentAlerts = [];
      renderDashboard();
      renderActivity();
    }
  } finally {
    if (requestId === state.dashboardRequests.bootstrap) {
      workspace?.removeAttribute("aria-busy");
    }
  }
}

async function loadRecentAlerts(targetId) {
  const requestId = state.dashboardRequests.alerts + 1;
  state.dashboardRequests.alerts = requestId;
  const params = new URLSearchParams({
    limit: "6",
    target_id: String(targetId),
    kind: "seat_increases",
    notifications_only: "true",
  });
  let data;
  try {
    data = await api(`/api/v1/activity?${params.toString()}`);
  } catch (error) {
    if (requestId !== state.dashboardRequests.alerts || state.selectedTargetId !== targetId) return;
    throw error;
  }
  if (requestId !== state.dashboardRequests.alerts || state.selectedTargetId !== targetId) return;
  const newAlerts = Array.isArray(data.items)
    ? data.items
    : Array.isArray(data.activity) ? data.activity : [];

  state.recentAlerts = newAlerts;
  renderActivity();
}

async function loadScreenings(targetId, { fallbackMarker = null } = {}) {
  const requestId = state.dashboardRequests.screenings + 1;
  state.dashboardRequests.screenings = requestId;
  let data;
  try {
    data = await api(`/api/v1/targets/${targetId}/screenings`);
  } catch (error) {
    if (requestId !== state.dashboardRequests.screenings || state.selectedTargetId !== targetId) return;
    throw error;
  }
  if (requestId !== state.dashboardRequests.screenings || state.selectedTargetId !== targetId) return;
  const marker = focusMarker() || fallbackMarker;
  state.screenings = data.screenings || [];
  renderDashboard();
  restoreFocusIfLost(marker);
}

async function selectTarget(id) {
  const marker = focusMarker();
  if (state.selectedTargetId !== id) {
    state.pollSettingsDirtyTargetId = null;
    state.bulkThresholdDirtyTargetId = null;
  }
  state.selectedTargetId = id;
  state.screenings = [];
  state.recentAlerts = [];
  pollSettingsFeedback();
  bulkThresholdFeedback();
  renderTargets();
  restoreFocusIfLost(marker);
  renderDashboard();
  renderActivity();
  await Promise.all([loadScreenings(id), loadRecentAlerts(id)]);
}

function deleteTargetFeedback(message = "") {
  const feedback = byId("deleteTargetFeedback");
  feedback.hidden = !message;
  feedback.textContent = message;
  feedback.className = "form-feedback is-error";
}

function openDeleteTargetDialog() {
  const target = selectedTarget();
  if (!target) return;
  deleteTargetFeedback();
  byId("deleteTargetName").textContent = `${target.site_name} · ${target.movie_name} · ${targetFormatLabel(target)}`;
  showDialog(byId("deleteTargetDialog"), byId("confirmDeleteTarget"));
}

async function deleteCurrentTarget(event) {
  event.preventDefault();
  const target = selectedTarget();
  if (!target) return;
  const button = byId("confirmDeleteTarget");
  const originalLabel = button.textContent;
  setButtonBusy(button, true);
  button.textContent = "삭제 중…";
  deleteTargetFeedback();
  try {
    let result;
    try {
      result = await api(`/api/v1/targets/${target.id}`, {
        method: "DELETE",
        body: { version: target.version },
      });
    } catch (error) {
      deleteTargetFeedback(errorMessage(error));
      return;
    }
    const deleted = recordOrEmpty(result.deleted);
    const successMessage = `감시 대상과 기록을 삭제했습니다 · 회차 ${Number(deleted.screenings) || 0}개 · 좌석 이력 ${Number(deleted.seat_history) || 0}건 · 알림 ${Number(deleted.notifications) || 0}건`;
    byId("deleteTargetDialog").close();
    state.targets = state.targets.filter((item) => item.id !== target.id);
    state.selectedTargetId = state.targets[0]?.id || null;
    state.screenings = [];
    state.recentAlerts = [];
    state.pollSettingsDirtyTargetId = null;
    state.bulkThresholdDirtyTargetId = null;
    renderTargets();
    renderActivityTargetFilter();
    renderDashboard();
    renderActivity();
    showToast(successMessage);
    try {
      await loadBootstrap({ preserveSelection: false });
    } catch (refreshError) {
      reportBackgroundError(refreshError);
      showToast(`삭제는 완료됐지만 최신 화면을 불러오지 못했습니다. 새로고침해 주세요. · ${errorMessage(refreshError)}`, true);
    }
  } finally {
    setButtonBusy(button, false);
    button.textContent = originalLabel;
  }
}

async function patchTarget(values) {
  const target = selectedTarget();
  if (!target) return;
  const marker = focusMarker();
  const controls = [byId("targetEnabled"), byId("notifyNew"), byId("autoTrackNew")];
  const container = document.querySelector(".hero-controls");
  controls.forEach((control) => { control.disabled = true; });
  container?.setAttribute("aria-busy", "true");
  try {
    await api(`/api/v1/targets/${target.id}`, { method: "PATCH", body: { ...values, version: target.version } });
    await loadBootstrap();
  } catch (error) {
    renderDashboard();
    throw error;
  } finally {
    controls.forEach((control) => { control.disabled = false; });
    container?.removeAttribute("aria-busy");
    restoreFocusIfLost(marker);
  }
}

async function updateScreeningWatch(screeningId, values, marker = null) {
  const data = await api(`/api/v1/screenings/${screeningId}/watch`, {
    method: "PUT",
    body: values,
  });
  const watch = data.watch || {};
  if (values.seat_change_threshold !== undefined) {
    state.screeningThresholdDrafts.delete(String(screeningId));
  }
  const targetId = Number(watch.target_id);
  if (Number(state.selectedTargetId) !== targetId) {
    return { watch, synchronized: true };
  }
  state.screenings = state.screenings.map((item) => (
    String(item.id) === String(screeningId)
      ? {
          ...item,
          watched: Boolean(watch.enabled),
          seat_change_threshold: Number(watch.seat_change_threshold) || item.seat_change_threshold,
        }
      : item
  ));
  renderDashboard();
  restoreFocusIfLost(marker);
  try {
    await loadScreenings(targetId, { fallbackMarker: marker });
    return { watch, synchronized: true };
  } catch (error) {
    console.error(error);
    return { watch, synchronized: false };
  }
}

async function saveScreeningThreshold(screeningId) {
  const input = document.querySelector(`[data-threshold-id="${CSS.escape(String(screeningId))}"]`);
  const item = state.screenings.find((screening) => String(screening.id) === String(screeningId));
  if (!(input instanceof HTMLInputElement) || !item) return;
  if (!input.reportValidity() || !Number.isInteger(input.valueAsNumber)) return;
  const threshold = input.valueAsNumber;
  const marker = { key: "thresholdId", value: String(screeningId) };
  const button = document.querySelector(`[data-threshold-save-id="${CSS.escape(String(screeningId))}"]`);
  input.disabled = true;
  if (button instanceof HTMLButtonElement) setButtonBusy(button, true);
  try {
    const result = await updateScreeningWatch(screeningId, {
      enabled: Boolean(item.watched),
      seat_change_threshold: threshold,
    }, marker);
    showToast(
      result.synchronized
        ? `${displayDate(item.screening_date)} ${displayTime(item.start_time)} · +${threshold}석부터 알립니다.`
        : "기준은 저장됐지만 최신 목록을 불러오지 못했습니다. 자동으로 다시 확인합니다.",
      !result.synchronized,
    );
  } catch (error) {
    input.disabled = false;
    if (button instanceof HTMLButtonElement) setButtonBusy(button, false);
    throw error;
  }
}

async function applyBulkThreshold(event) {
  event.preventDefault();
  const target = selectedTarget();
  const form = byId("bulkThresholdForm");
  const input = byId("bulkThreshold");
  if (!target || !form.reportValidity() || !Number.isInteger(input.valueAsNumber)) return;
  const threshold = input.valueAsNumber;
  const trackedCount = state.screenings.filter((item) => item.watched).length;
  const button = byId("applyBulkThreshold");
  const originalLabel = button.textContent;
  setButtonBusy(button, true);
  input.disabled = true;
  bulkThresholdFeedback("알림 회차에 기준을 적용하는 중입니다.", "pending");
  try {
    await api(`/api/v1/targets/${target.id}/watches`, {
      method: "PUT",
      body: { seat_change_threshold: threshold },
    });
    if (state.selectedTargetId !== target.id) return;
    state.bulkThresholdDirtyTargetId = null;
    for (const item of state.screenings) {
      if (item.watched) state.screeningThresholdDrafts.delete(String(item.id));
    }
    state.screenings = state.screenings.map((item) => (
      item.watched ? { ...item, seat_change_threshold: threshold } : item
    ));
    renderDashboard();
    let synchronized = true;
    try {
      await loadScreenings(target.id);
    } catch (error) {
      console.error(error);
      synchronized = false;
    }
    bulkThresholdFeedback(
      synchronized
        ? `알림 회차 ${trackedCount}개에 +${threshold}석 기준을 적용했습니다.`
        : `기준은 ${trackedCount}개 회차에 적용됐지만 최신 목록을 불러오지 못했습니다.`,
      synchronized ? "success" : "error",
    );
    showToast(
      synchronized
        ? `잔여석 증가 알림 기준을 +${threshold}석으로 일괄 적용했습니다.`
        : "일괄 기준은 저장됐습니다. 화면은 자동으로 다시 확인합니다.",
      !synchronized,
    );
  } catch (error) {
    input.disabled = false;
    bulkThresholdFeedback(`일괄 적용하지 못했습니다. ${errorMessage(error)}`, "error");
    throw error;
  } finally {
    button.textContent = originalLabel;
    setButtonBusy(button, false);
    const currentTarget = selectedTarget();
    if (currentTarget) renderBulkThreshold(currentTarget);
  }
}

async function savePollSettings(event) {
  event.preventDefault();
  const target = selectedTarget();
  const form = byId("pollSettingsForm");
  if (!target || !form.reportValidity()) return;
  const interval = byId("pollInterval").valueAsNumber;
  const jitter = byId("pollJitter").valueAsNumber;
  if (!Number.isInteger(interval) || !Number.isInteger(jitter)) {
    pollSettingsFeedback("허용 범위 안의 정수를 입력하세요.", "error");
    return;
  }
  const saveButton = byId("savePollSettings");
  const originalLabel = saveButton.textContent;
  const marker = focusMarker();
  setButtonBusy(saveButton, true);
  saveButton.textContent = "저장 중…";
  form.setAttribute("aria-busy", "true");
  pollSettingsFeedback("조회 간격을 저장하고 있습니다.", "pending");
  try {
    const result = await api(`/api/v1/targets/${target.id}`, {
      method: "PATCH",
      body: {
        version: target.version,
        poll_interval_seconds: interval,
        poll_jitter_seconds: jitter,
      },
    });
    state.targets = state.targets.map((item) => item.id === target.id ? result.target : item);
    state.pollSettingsDirtyTargetId = null;
    renderDashboard();
    pollSettingsFeedback(`저장 완료 · 조회마다 ${interval}~${interval + jitter}초 사이의 간격을 사용합니다.`, "success");
    showToast("조회 간격을 저장했습니다.");
  } catch (error) {
    console.error(error);
    pollSettingsFeedback(`조회 간격을 저장하지 못했습니다. ${errorMessage(error)}`, "error");
  } finally {
    form.removeAttribute("aria-busy");
    setButtonBusy(saveButton, false);
    saveButton.textContent = originalLabel;
    updatePollSettingsPreview();
    restoreFocusIfLost(marker);
  }
}

async function refreshCurrent() {
  const target = selectedTarget();
  if (!target || refreshPhase(target) !== "idle" || state.refreshing) return;
  const marker = focusMarker();
  state.refreshing = true;
  syncRefreshButton(target);
  try {
    const result = await api(`/api/v1/targets/${target.id}/refresh`, { method: "POST" });
    state.targets = state.targets.map((item) => item.id === target.id ? result.target : item);
    state.refreshRequest = {
      targetId: target.id,
      requestedAt: timestampMs(result.target?.refresh_requested_at) ?? Date.now() + state.serverClockOffsetMs,
    };
    renderDashboard();
    showToast("즉시 조회를 요청했습니다. 잠시 후 결과가 갱신됩니다.");
    setTimeout(() => loadBootstrap().catch(reportError), 1500);
    setTimeout(() => loadBootstrap().catch(reportError), 4000);
  } finally {
    state.refreshing = false;
    syncRefreshButton(selectedTarget());
    restoreFocusIfLost(marker);
  }
}

async function openTargetDialog() {
  showDialog(byId("targetDialog"), byId("siteSelect"));
  if (state.catalog) return;
  try {
    const data = await api("/api/v1/catalog/sites");
    state.catalog = data.regions || [];
    const options = ['<option value="">극장을 선택하세요</option>'];
    for (const region of state.catalog) {
      options.push(`<optgroup label="${escapeHtml(region.name)}">`);
      for (const site of region.sites || []) {
        const unavailable = site.status && site.status !== "운영중";
        options.push(`<option value="${escapeHtml(site.site_no)}" data-site-name="${escapeHtml(site.site_name)}" ${unavailable ? "disabled" : ""}>${escapeHtml(site.site_name)}${unavailable ? ` · ${escapeHtml(site.status)}` : ""}</option>`);
      }
      options.push("</optgroup>");
    }
    byId("siteSelect").innerHTML = options.join("");
  } catch (error) {
    reportError(error);
    byId("siteSelect").innerHTML = '<option value="">극장 목록 조회 실패</option>';
  }
}

async function loadMovies(siteNo) {
  const movieSelect = byId("movieSelect");
  state.catalogMovies = [];
  movieSelect.disabled = true;
  byId("createTarget").disabled = true;
  resetFormatSelect();
  if (!siteNo) {
    movieSelect.innerHTML = '<option value="">먼저 극장을 선택하세요</option>';
    updateSelectionPreview();
    return;
  }
  movieSelect.innerHTML = '<option value="">영화 조회 중…</option>';
  try {
    const data = await api(`/api/v1/catalog/sites/${encodeURIComponent(siteNo)}/movies`);
    if (byId("siteSelect").value !== siteNo) return;
    const movies = Array.isArray(data.movies) ? data.movies : [];
    state.catalogMovies = movies;
    movieSelect.innerHTML = movies.length
      ? '<option value="">영화를 선택하세요</option>' + movies.map((movie) => `<option value="${escapeHtml(movie.movie_no)}" data-movie-name="${escapeHtml(movie.movie_name)}">${escapeHtml(movie.movie_name)} · ${(movie.dates || []).length}일</option>`).join("")
      : '<option value="">현재 확인되는 상영 영화가 없습니다</option>';
    movieSelect.disabled = !movies.length;
    updateSelectionPreview();
  } catch (error) {
    if (byId("siteSelect").value !== siteNo) return;
    reportError(error);
    state.catalogMovies = [];
    movieSelect.innerHTML = '<option value="">영화 목록 조회 실패</option>';
    updateSelectionPreview();
  }
}

function resetFormatSelect(message = "먼저 영화를 선택하세요") {
  const formatSelect = byId("formatSelect");
  formatSelect.disabled = true;
  formatSelect.innerHTML = `<option value="">${escapeHtml(message)}</option>`;
}

function selectedCatalogMovie() {
  const movieNo = byId("movieSelect").value;
  return state.catalogMovies.find((movie) => String(movie.movie_no) === movieNo) || null;
}

function selectedCatalogFormat() {
  const movie = selectedCatalogMovie();
  const selectedValue = byId("formatSelect").value;
  if (!movie || selectedValue === "") return null;
  const selectedIndex = Number(selectedValue);
  if (!Number.isInteger(selectedIndex)) return null;
  const format = Array.isArray(movie.formats) ? movie.formats[selectedIndex] : null;
  const formatCode = String(format?.format_code || "").trim();
  const formatName = String(format?.format_name || "").trim();
  return format && formatCode && formatName ? format : null;
}

function loadFormats(movieNo) {
  const formatSelect = byId("formatSelect");
  byId("createTarget").disabled = true;
  resetFormatSelect();
  const movie = state.catalogMovies.find((item) => String(item.movie_no) === String(movieNo));
  if (!movie) {
    updateSelectionPreview();
    return;
  }
  const formats = Array.isArray(movie.formats) ? movie.formats : [];
  const options = formats.flatMap((format, index) => {
    const formatCode = String(format?.format_code || "").trim();
    const formatName = String(format?.format_name || "").trim();
    if (!formatCode || !formatName) return [];
    const dateCount = Array.isArray(format.screening_dates) ? format.screening_dates.length : 0;
    return [`<option value="${index}">${escapeHtml(formatName)} · ${dateCount}일</option>`];
  });
  formatSelect.innerHTML = options.length
    ? '<option value="">상영 포맷을 선택하세요</option>' + options.join("")
    : '<option value="">현재 확인되는 상영 포맷이 없습니다</option>';
  formatSelect.disabled = !options.length;
  updateSelectionPreview();
}

function targetSelection() {
  const siteOption = byId("siteSelect").selectedOptions[0];
  const movieOption = byId("movieSelect").selectedOptions[0];
  const format = selectedCatalogFormat();
  if (!siteOption?.value || !movieOption?.value || !format) return null;
  return { siteOption, movieOption, format };
}

function updateSelectionPreview() {
  const selection = targetSelection();
  byId("createTarget").disabled = !selection;
  if (!selection) {
    byId("selectionPreview").innerHTML = '<span aria-hidden="true">◌</span><p>현재 CGV 시간표에서 확인되는 영화와 상영 포맷을 표시합니다.</p>';
    return;
  }
  const { siteOption, movieOption, format } = selection;
  const screenNames = Array.isArray(format.screen_names) ? format.screen_names.filter(Boolean) : [];
  const dates = Array.isArray(format.screening_dates) ? format.screening_dates : [];
  const visibleScreenNames = screenNames.slice(0, 3);
  const screenSummary = visibleScreenNames.length
    ? `${visibleScreenNames.join(", ")}${screenNames.length > visibleScreenNames.length ? ` 외 ${screenNames.length - visibleScreenNames.length}개` : ""}`
    : "상영관 정보 미확인";
  const detail = [`${dates.length}일`, screenSummary].join(" · ");
  byId("selectionPreview").innerHTML = `<span aria-hidden="true">◎</span><p><strong>${escapeHtml(siteOption.dataset.siteName)}</strong><br>${escapeHtml(movieOption.dataset.movieName)} · ${escapeHtml(format.format_name)}<br>${escapeHtml(detail)}</p>`;
}

async function createTarget(event) {
  event.preventDefault();
  const selection = targetSelection();
  if (!selection) return;
  const { siteOption, movieOption, format } = selection;
  const createButton = byId("createTarget");
  setButtonBusy(createButton, true);
  try {
    const result = await api("/api/v1/targets", {
      method: "POST",
      body: {
        site_no: siteOption.value,
        site_name: siteOption.dataset.siteName,
        movie_no: movieOption.value,
        movie_name: movieOption.dataset.movieName,
        format_code: String(format.format_code),
        format_name: String(format.format_name),
      },
    });
    byId("targetDialog").close();
    state.selectedTargetId = result.target.id;
    await loadBootstrap();
    await refreshCurrent();
  } finally {
    createButton.setAttribute("aria-busy", "false");
    createButton.disabled = !targetSelection();
  }
}

function openTelegramDialog() {
  byId("botToken").value = "";
  byId("chatCandidates").innerHTML = "";
  telegramFeedback();
  renderTelegram();
  showDialog(
    byId("telegramDialog"),
    state.telegram.configured ? byId("chatId") : byId("botToken"),
  );
}

async function saveTelegram(event) {
  event.preventDefault();
  const tokenInput = byId("botToken");
  const chatInput = byId("chatId");
  const token = tokenInput.value.trim();
  const chatId = chatInput.value.trim();
  if (!chatId) {
    telegramFeedback("알림 받을 Chat ID를 입력하거나 채팅 목록에서 선택하세요.", "error");
    chatInput.focus();
    chatInput.reportValidity();
    return;
  }
  if (!state.telegram.configured && !token) {
    telegramFeedback("처음 연결할 때는 Bot API token을 입력하세요.", "error");
    tokenInput.focus();
    tokenInput.reportValidity();
    return;
  }
  if ((token && !tokenInput.checkValidity()) || !chatInput.checkValidity()) {
    const invalidInput = !tokenInput.checkValidity() ? tokenInput : chatInput;
    telegramFeedback("Telegram 설정 입력값을 확인하세요.", "error");
    invalidInput.focus();
    invalidInput.reportValidity();
    return;
  }
  const marker = focusMarker();
  const saveButton = byId("saveTelegram");
  const originalLabel = saveButton.textContent;
  setButtonBusy(saveButton, true);
  saveButton.textContent = "저장 중…";
  telegramFeedback("저장 중… Telegram에서 봇 정보를 확인하고 있습니다.", "pending");
  const payload = {
    chat_id: chatId,
    enabled: byId("telegramEnabled").checked,
    version: state.telegram.version || undefined,
  };
  try {
    if (token) payload.bot_token = token;
    const result = await api("/api/v1/telegram", { method: "PUT", body: payload });
    state.telegram = result.telegram;
    renderTelegram();
    byId("botToken").value = "";
    const botLabel = state.telegram.bot_username ? `@${state.telegram.bot_username}` : "Telegram 봇";
    const enabledLabel = state.telegram.enabled ? "알림 활성화" : "알림 일시 중지";
    telegramFeedback(`저장 완료 · ${botLabel} · Chat ID ${state.telegram.chat_id_masked || "저장됨"} · ${enabledLabel}`, "success");
    showToast("Telegram 설정을 저장했습니다.");
  } catch (error) {
    console.error(error);
    telegramFeedback(`저장하지 못했습니다. ${errorMessage(error)}`, "error");
    byId("telegramFeedback").focus({ preventScroll: true });
  } finally {
    setButtonBusy(saveButton, false);
    saveButton.textContent = originalLabel;
    restoreFocusIfLost(marker);
  }
}

async function loadChats() {
  const loadButton = byId("loadChats");
  if (loadButton.disabled) return;
  const marker = focusMarker();
  const token = byId("botToken").value.trim();
  const tokenInput = byId("botToken");
  if (!state.telegram.configured && !token) {
    telegramFeedback("처음 연결할 때는 Bot API token을 먼저 입력하세요.", "error");
    tokenInput.focus();
    tokenInput.reportValidity();
    return;
  }
  if (token && !tokenInput.checkValidity()) {
    telegramFeedback("Bot API token 형식을 확인하세요.", "error");
    tokenInput.focus();
    tokenInput.reportValidity();
    return;
  }
  const originalLabel = loadButton.textContent;
  setButtonBusy(loadButton, true);
  loadButton.textContent = "찾는 중…";
  telegramFeedback("Telegram에서 최근 채팅을 조회하고 있습니다.", "pending");
  try {
    const data = await api("/api/v1/telegram/chats", {
      method: "POST",
      body: token ? { bot_token: token } : {},
    });
    const chats = data.chats || [];
    byId("chatCandidates").innerHTML = chats.length
      ? chats.map((chat) => `<button class="chat-option" data-chat-id="${escapeHtml(chat.id)}" type="button" aria-pressed="false" aria-label="${escapeHtml(chat.title || chat.type || "채팅")} ${escapeHtml(chat.id)} 선택"><span>${escapeHtml(chat.title || chat.type || "채팅")}</span><strong>${escapeHtml(chat.id)}</strong></button>`).join("")
      : '<p class="security-note">채팅이 없습니다. 봇에게 /start를 보낸 뒤 다시 시도하세요.</p>';
    telegramFeedback(chats.length ? `${chats.length}개의 채팅을 찾았습니다. 아래에서 알림 받을 채팅을 선택하세요.` : "채팅을 찾지 못했습니다. 봇에게 /start를 보낸 뒤 다시 시도하세요.", chats.length ? "success" : "info");
  } catch (error) {
    console.error(error);
    telegramFeedback(`채팅을 찾지 못했습니다. ${errorMessage(error)}`, "error");
    byId("telegramFeedback").focus({ preventScroll: true });
  } finally {
    setButtonBusy(loadButton, false);
    loadButton.textContent = originalLabel;
    restoreFocusIfLost(marker);
  }
}

async function testTelegram() {
  const testButton = byId("testTelegram");
  if (testButton.disabled) return;
  const marker = focusMarker();
  const originalLabel = testButton.textContent;
  setButtonBusy(testButton, true);
  testButton.textContent = "전송 중…";
  telegramFeedback("저장된 설정으로 시험 메시지를 전송하고 있습니다.", "pending");
  try {
    await api("/api/v1/telegram/test", { method: "POST" });
    telegramFeedback("시험 메시지를 전송했습니다. Telegram 앱에서 수신 여부를 확인하세요.", "success");
    showToast("저장된 Telegram 설정으로 시험 메시지를 보냈습니다.");
  } catch (error) {
    console.error(error);
    telegramFeedback(`시험 메시지를 보내지 못했습니다. ${errorMessage(error)}`, "error");
    byId("telegramFeedback").focus({ preventScroll: true });
  } finally {
    testButton.setAttribute("aria-busy", "false");
    testButton.disabled = !state.telegram.configured || !state.telegram.enabled;
    testButton.textContent = originalLabel;
    restoreFocusIfLost(marker);
  }
}

function reportError(error) {
  console.error(error);
  showToast(error.message || "처리 중 오류가 발생했습니다.", true);
}

document.addEventListener("click", (event) => {
  const targetButton = event.target.closest("[data-target-id]");
  if (targetButton) selectTarget(Number(targetButton.dataset.targetId)).catch(reportError);

  const watchButton = event.target.closest("[data-watch-id]");
  if (watchButton) {
    const marker = { key: "watchId", value: watchButton.dataset.watchId };
    const thresholdInput = document.querySelector(`[data-threshold-id="${CSS.escape(watchButton.dataset.watchId)}"]`);
    const body = { enabled: watchButton.dataset.watchEnabled === "true" };
    if (thresholdInput instanceof HTMLInputElement
        && thresholdInput.checkValidity()
        && Number.isInteger(thresholdInput.valueAsNumber)) {
      body.seat_change_threshold = thresholdInput.valueAsNumber;
    }
    setButtonBusy(watchButton, true);
    updateScreeningWatch(watchButton.dataset.watchId, body, marker)
      .then((result) => {
        if (!result.synchronized) {
          showToast("알림 설정은 저장됐습니다. 화면은 자동으로 다시 확인합니다.", true);
        }
      })
      .catch((error) => {
        setButtonBusy(watchButton, false);
        restoreFocusIfLost(marker);
        reportError(error);
      });
  }

  const thresholdButton = event.target.closest("[data-threshold-save-id]");
  if (thresholdButton) {
    saveScreeningThreshold(thresholdButton.dataset.thresholdSaveId).catch(reportError);
  }

  const filterButton = event.target.closest("[data-filter]");
  if (filterButton) {
    state.filter = filterButton.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((button) => {
      const active = button === filterButton;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    renderSchedule();
  }

  const chatButton = event.target.closest("[data-chat-id]");
  if (chatButton) {
    byId("chatId").value = chatButton.dataset.chatId;
    document.querySelectorAll("[data-chat-id]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button === chatButton));
    });
    telegramFeedback("채팅을 선택했습니다. 설정 저장을 눌러 적용하세요.", "info");
  }
});

document.addEventListener("toggle", (event) => {
  const details = event.target;
  if (!(details instanceof HTMLDetailsElement)) return;
  if (details.dataset.screeningHistoryId) {
    const collection = state.expandedScreenings;
    if (details.open) collection.add(details.dataset.screeningHistoryId);
    else collection.delete(details.dataset.screeningHistoryId);
  }
  if (details.dataset.activityDetailId) {
    const collection = state.expandedActivities;
    if (details.open) collection.add(details.dataset.activityDetailId);
    else collection.delete(details.dataset.activityDetailId);
  }
}, true);

document.addEventListener("keydown", (event) => {
  const input = event.target.closest?.("[data-threshold-id]");
  if (input && event.key === "Enter") {
    event.preventDefault();
    saveScreeningThreshold(input.dataset.thresholdId).catch(reportError);
  }
});

document.addEventListener("input", (event) => {
  const input = event.target.closest?.("[data-threshold-id]");
  if (input instanceof HTMLInputElement) {
    state.screeningThresholdDrafts.set(String(input.dataset.thresholdId), input.value);
  }
});

document.querySelectorAll(".dialog-close").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
});

document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("close", () => {
    if (dialogReturnFocus?.isConnected) dialogReturnFocus.focus();
    dialogReturnFocus = null;
  });
});

byId("addTarget").addEventListener("click", () => openTargetDialog().catch(reportError));
byId("emptyAddTarget").addEventListener("click", () => openTargetDialog().catch(reportError));
byId("openTelegram").addEventListener("click", openTelegramDialog);
byId("telegramMiniAction").addEventListener("click", openTelegramDialog);
byId("webPushMiniAction").addEventListener("click", openWebPushDialog);
byId("openDeleteTarget").addEventListener("click", openDeleteTargetDialog);
byId("refreshNow").addEventListener("click", () => refreshCurrent().catch(reportError));
byId("targetEnabled").addEventListener("change", (event) => patchTarget({ enabled: event.target.checked }).catch(reportError));
byId("notifyNew").addEventListener("change", (event) => patchTarget({ notify_new: event.target.checked }).catch(reportError));
byId("autoTrackNew").addEventListener("change", (event) => patchTarget({ auto_track_new: event.target.checked }).catch(reportError));
byId("siteSelect").addEventListener("change", (event) => loadMovies(event.target.value).catch(reportError));
byId("movieSelect").addEventListener("change", (event) => loadFormats(event.target.value));
byId("formatSelect").addEventListener("change", updateSelectionPreview);
byId("targetForm").addEventListener("submit", (event) => createTarget(event).catch(reportError));
byId("deleteTargetForm").addEventListener("submit", (event) => deleteCurrentTarget(event).catch(reportError));
byId("bulkThresholdForm").addEventListener("submit", (event) => applyBulkThreshold(event).catch(reportError));
byId("pollSettingsForm").addEventListener("submit", (event) => savePollSettings(event).catch(reportError));
byId("bulkThreshold").addEventListener("input", (event) => {
  const target = selectedTarget();
  state.bulkThresholdDirtyTargetId = target?.id || null;
  byId("applyBulkThreshold").disabled = !target
    || !event.target.checkValidity()
    || !event.target.value
    || state.screenings.every((item) => !item.watched);
  bulkThresholdFeedback("변경사항이 아직 적용되지 않았습니다.", "info");
});
[byId("pollInterval"), byId("pollJitter")].forEach((input) => {
  input.addEventListener("input", () => updatePollSettingsPreview({ announce: true }));
});
byId("telegramForm").addEventListener("submit", (event) => saveTelegram(event).catch(reportError));
byId("loadChats").addEventListener("click", () => loadChats().catch(reportError));
byId("testTelegram").addEventListener("click", () => testTelegram().catch(reportError));
byId("enableWebPush").addEventListener("click", () => enableWebPush().catch(reportError));
byId("disableWebPush").addEventListener("click", () => disableWebPush().catch(reportError));
byId("testWebPush").addEventListener("click", () => testWebPush().catch(reportError));
byId("activityFilterForm").addEventListener("submit", (event) => applyActivityFilters(event).catch(reportError));
byId("resetActivityFilters").addEventListener("click", () => resetActivityFilters().catch(reportError));
byId("prevActivityPage").addEventListener("click", () => changeActivityPage(-1).catch(reportError));
byId("nextActivityPage").addEventListener("click", () => changeActivityPage(1).catch(reportError));
byId("showLatestActivity")?.addEventListener("click", () => showLatestActivity().catch(reportError));

[byId("botToken"), byId("chatId")].forEach((input) => {
  input.addEventListener("input", () => telegramFeedback("변경사항이 아직 저장되지 않았습니다.", "info"));
});
byId("telegramEnabled").addEventListener("change", () => {
  telegramFeedback("변경사항이 아직 저장되지 않았습니다.", "info");
});

initializePathView();
updateLiveTimes();
loadBootstrap({ preserveSelection: false })
  .then(async () => {
    await initializeWebPush();
    await monitorIncreaseAlerts();
    if (activityPath) await loadActivityPage(null, { page: 1, reset: true });
  })
  .catch(reportError);
setInterval(updateLiveTimes, 1000);
setInterval(() => {
  syncLiveView().catch(reportBackgroundError);
  monitorIncreaseAlerts().catch(reportBackgroundError);
}, liveSyncIntervalMs);
setInterval(() => {
  const workspace = document.querySelector(".workspace");
  if (!activityPath
      && !document.hidden
      && !document.querySelector("dialog[open]")
      && !state.liveSync.inFlight
      && workspace?.getAttribute("aria-busy") !== "true") {
    loadBootstrap().catch(reportBackgroundError);
  }
}, 10000);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) syncLiveView({ force: true }).catch(reportBackgroundError);
});
