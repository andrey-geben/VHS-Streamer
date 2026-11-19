// VHS-Streamer Service Worker
// Кэширует только статику. Никогда: API, WebRTC, архив, internal.

const CACHE_NAME = 'vhs-streamer-v2';
const STATIC_ASSETS = [
  '/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-192-maskable.png',
  '/static/icons/icon-512-maskable.png',
  '/static/icons/apple-touch-icon.png',
  '/favicon.ico',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      for (const url of STATIC_ASSETS) {
        try { await cache.add(url); } catch (e) { /* пропускаем */ }
      }
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Никогда не кэшируем: API, WebRTC, архив, внутренние
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/internal/') ||
    url.pathname.startsWith('/video_feed/') ||
    url.pathname.startsWith('/archive/') ||
    url.port === '8889'
  ) {
    return;
  }

  // Cache-first для статики
  if (
    url.pathname.startsWith('/static/') ||
    url.pathname === '/manifest.json' ||
    url.pathname === '/favicon.ico'
  ) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((resp) => {
          if (resp.ok && event.request.method === 'GET') {
            const copy = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(event.request, copy));
          }
          return resp;
        });
      })
    );
  }
});