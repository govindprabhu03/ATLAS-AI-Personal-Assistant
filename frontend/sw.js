/* ATLAS service worker — fresh UI (network-first HTML), offline-capable shell. */
const CACHE = 'atlas-v9';
const SHELL = ['/', '/static/icon-192.png', '/static/icon-512.png',
               '/manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;            // POST/PATCH/etc. -> network
  if (url.pathname.startsWith('/api/')) return;      // live data -> network only

  // HTML / navigations: network-first so UI updates are never stale; fall back
  // to the cached shell when offline.
  if (e.request.mode === 'navigate' || url.pathname === '/') {
    e.respondWith(
      fetch(e.request).then(resp => {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put('/', copy));
        return resp;
      }).catch(() => caches.match('/'))
    );
    return;
  }

  // Static assets (icons, manifest): cache-first for instant loads.
  e.respondWith(
    caches.match(e.request).then(hit => hit ||
      fetch(e.request).then(resp => {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return resp;
      }).catch(() => caches.match('/')))
  );
});
