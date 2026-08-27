/* Service worker.
 *
 * Two jobs, and deliberately no more:
 *
 *   1. make the app installable to the home screen - which on iPhone is not
 *      cosmetic, because web push only works once it is installed
 *   2. keep the shell and already-seen thumbnails available on a bad
 *      connection, so the app opens and her gallery is there even on the
 *      underground
 *
 * What it must NOT do: cache API responses or generation results. Serving a
 * stale preview grid from cache would show her options that no longer exist,
 * and she would tap them and get nothing.
 */

const SHELL = 'estudio-shell-v1';
const MEDIA = 'estudio-media-v1';

const SHELL_ASSETS = ['/static/app.css', '/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL)
      .then(cache => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key !== SHELL && key !== MEDIA)
            .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never cache anything that changes: sessions, event streams, uploads.
  if (url.pathname.startsWith('/events/') ||
      url.pathname.startsWith('/previews') ||
      url.pathname.startsWith('/finals') ||
      url.pathname.startsWith('/upload') ||
      url.pathname.startsWith('/health')) {
    return;
  }

  // Images are content-addressed and immutable, so cache-first is safe and
  // makes the gallery instant on a second visit.
  if (url.pathname.startsWith('/media/')) {
    event.respondWith(
      caches.open(MEDIA).then(async (cache) => {
        const hit = await cache.match(request);
        if (hit) return hit;
        try {
          const response = await fetch(request);
          if (response.ok) cache.put(request, response.clone());
          return response;
        } catch (error) {
          return hit || Response.error();
        }
      })
    );
    return;
  }

  // Everything else: network first, falling back to cache so the app still
  // opens offline rather than showing the browser's error page.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && url.pathname.startsWith('/static/')) {
          const copy = response.clone();
          caches.open(SHELL).then(cache => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then(hit => hit || caches.match('/')))
  );
});

self.addEventListener('push', (event) => {
  let payload = { title: 'Estudio', body: 'Tus fotos están listas' };
  try { payload = { ...payload, ...event.data.json() }; } catch (error) {}

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      data: { url: payload.url || '/' },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification.data?.url || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((windows) => {
        for (const client of windows) {
          if (client.url.includes(target) && 'focus' in client) return client.focus();
        }
        return self.clients.openWindow(target);
      })
  );
});
