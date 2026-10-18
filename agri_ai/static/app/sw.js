// Offline shell for the farmer app: the UI loads with no network; API calls always go to the network.
const CACHE = "agri-shell-v1";
const SHELL = ["/app", "/static/app/manifest.webmanifest", "/static/app/icon.svg"];

self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())
));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  // Network first so updates arrive; fall back to the cached shell when offline.
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok && SHELL.includes(url.pathname)) caches.open(CACHE).then(c => c.put(e.request, r.clone()));
      return r;
    }).catch(() => caches.match(e.request).then(r => r || caches.match("/app")))
  );
});
