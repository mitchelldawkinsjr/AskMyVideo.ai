/* Recall AI service worker — mirrors prod PWA shell caching (fasted_calendar_pwa). */

const CACHE_NAME = "recall-ai-v1";
const PRECACHE_URLS = [
  "/static/video_processor/pwa/icons/icon-192.png",
  "/static/video_processor/pwa/icons/icon-512.png",
  "/static/video_processor/pwa/icons/apple-touch-icon.png",
  "/static/video_processor/js/recall-common.js",
  "/offline/",
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

function sameOrigin(url) {
  return url.origin === self.location.origin;
}

function isNetworkOnlyPath(pathname) {
  return (
    pathname.startsWith("/api/") ||
    pathname.startsWith("/admin/") ||
    pathname.startsWith("/media/") ||
    pathname.startsWith("/video-file/")
  );
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  if (!sameOrigin(url) || request.method !== "GET") {
    return;
  }

  if (isNetworkOnlyPath(url.pathname)) {
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone();
              caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
            }
            return response;
          })
      )
    );
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match("/offline/").then((offline) => offline || caches.match("/"))
      )
    );
  }
});
