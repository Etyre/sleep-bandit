// Sleep Bandit service worker: lets the site install as a home-screen app and
// keeps the last plan readable with no signal. Everything is network-first --
// the cache is only ever a fallback, so a fresh deploy shows up on the next load.
const CACHE = "sb-v1";
const SHELL = ["/styles.css", "/app.js", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Which requests get a cached fallback: the page itself (any path -- the public
// root and the admin page are both the same shell), the static assets, and the
// one read the Tonight view needs. Writes and everything else stay network-only.
function cacheable(req, url) {
  if (req.method !== "GET") return false;
  if (req.mode === "navigate") return true;
  if (SHELL.includes(url.pathname) || url.pathname === "/manifest.webmanifest") return true;
  return url.pathname === "/api/state";
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin || !cacheable(e.request, url)) return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const res = await fetch(e.request);
      if (res.ok) cache.put(e.request, res.clone());
      return res;
    } catch (err) {
      const hit = await cache.match(e.request);
      if (!hit) throw err;
      // mark it so the page can say "offline -- last saved plan"
      const h = new Headers(hit.headers); h.set("X-SB-Cached", "1");
      return new Response(hit.body, { status: hit.status, statusText: hit.statusText, headers: h });
    }
  })());
});
