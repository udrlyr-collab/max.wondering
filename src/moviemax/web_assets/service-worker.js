"use strict";

function allowedDestination(value) {
  try {
    const url = new URL(value || "/", self.location.origin);
    const sameOrigin = url.origin === self.location.origin;
    return sameOrigin ? url.href : `${self.location.origin}/`;
  } catch (_error) {
    return `${self.location.origin}/`;
  }
}

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = {};
  }
  const title = typeof payload.title === "string" && payload.title
    ? payload.title
    : "MovieMax 알림";
  const body = typeof payload.body === "string" ? payload.body : "잔여석 변동을 확인하세요.";
  const tag = typeof payload.tag === "string" && payload.tag
    ? payload.tag
    : "moviemax-seat-increase";
  const url = allowedDestination(payload.url);
  event.waitUntil(self.registration.showNotification(title, {
    body,
    tag,
    renotify: false,
    data: { url },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = allowedDestination(event.notification.data?.url);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({
      type: "window",
      includeUncontrolled: true,
    });
    const destination = new URL(url);
    if (destination.origin === self.location.origin) {
      const existing = windows.find((client) => client.url === url);
      if (existing && "focus" in existing) return existing.focus();
    }
    return self.clients.openWindow ? self.clients.openWindow(url) : undefined;
  })());
});
