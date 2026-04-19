// sw.js – JarAI Service Worker
const CACHE_NAME = 'jarai-v1';
const ASSETS = ['/', '/index.html', '/manifest.json', '/icons/icon-192.png', '/icons/icon-512.png'];

// ── Install ───────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

// ── Activate ──────────────────────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── Fetch (cache-first for static assets) ────────────────────────────────────
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request))
  );
});

// ── Push notification received ────────────────────────────────────────────────
self.addEventListener('push', event => {
  let data = { title: '⏰ Reminder', body: 'Time to check your reminder!', lang: 'en' };

  try {
    data = event.data.json();
  } catch (_) {
    data.body = event.data?.text() || data.body;
  }

  const options = {
    body: data.body,
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-96.png',
    vibrate: [200, 100, 200, 100, 200],
    tag: data.id || 'jarai-reminder',
    renotify: true,
    requireInteraction: true,
    data: { lang: data.lang, text: data.body },
    actions: [
      { action: 'dismiss', title: 'Dismiss' },
    ],
  };

  event.waitUntil(self.registration.showNotification(data.title, options));
});

// ── Notification click → speak the reminder via TTS in the tab ───────────────
self.addEventListener('notificationclick', event => {
  event.notification.close();

  if (event.action === 'dismiss') return;

  const { lang, text } = event.notification.data || {};

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      // focus existing window or open a new one
      const existing = windowClients.find(c => c.focused || c.visibilityState === 'visible');
      const target   = existing || windowClients[0];

      if (target) {
        target.focus();
        target.postMessage({ type: 'SPEAK_REMINDER', text, lang });
        return;
      }
      return clients.openWindow('/').then(win => {
        if (win) {
          // slight delay so the page can load the message listener
          setTimeout(() => win.postMessage({ type: 'SPEAK_REMINDER', text, lang }), 1500);
        }
      });
    })
  );
});