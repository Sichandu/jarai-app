// sw.js — JarAI Service Worker
// Handles WebPush events + notification click → speaks reminder text

const CACHE_NAME = 'jarai-v2';

// ── Install / Activate ─────────────────────────────────────────────────────
self.addEventListener('install',  () => self.skipWaiting());
self.addEventListener('activate', e  => e.waitUntil(self.clients.claim()));

// ── Push event (WebPush fallback for desktop) ──────────────────────────────
// On Android you'll mostly get FCM, but this handles the WebPush path.
self.addEventListener('push', e => {
  let payload = {};
  try {
    payload = e.data?.json() || {};
  } catch (_) {
    payload = { title: '⏰ Reminder', body: e.data?.text() || '' };
  }

  // Normalise — payload can be { title, body, lang, id }
  // OR the full FCM-style { notification: {title,body}, data: {lang,id,body} }
  const title = payload.title
    || payload.notification?.title
    || '⏰ Reminder';

  const body  = payload.body
    || payload.data?.body
    || payload.notification?.body
    || '';          // ← Never "undefined" because we fall back to ''

  const lang  = payload.lang  || payload.data?.lang  || 'en';
  const id    = payload.id    || payload.data?.id    || '';

  e.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon:             '/icons/icon-192.png',
      badge:            '/icons/icon-192.png',
      tag:              id || 'jarai-reminder',
      requireInteraction: true,          // stays on screen until tapped (Android)
      vibrate:          [200, 100, 200, 100, 400],
      data:             { text: body, lang, id },
    })
  );
});

// ── Notification click ────────────────────────────────────────────────────
// When user taps the notification:
//   1. Close it
//   2. Focus / open the app window
//   3. Post a message → app calls speak() with the reminder text
self.addEventListener('notificationclick', e => {
  e.notification.close();

  const { text, lang } = e.notification.data || {};

  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      // Helper: post SPEAK message to a client
      const postSpeak = (client) => {
        if (text) {
          client.postMessage({ type: 'SPEAK_REMINDER', text, lang: lang || 'en' });
        }
      };

      // If app window already open → focus it and speak
      for (const client of clientList) {
        if ('focus' in client) {
          client.focus();
          postSpeak(client);
          return;
        }
      }

      // Otherwise open a new window, then speak once it's ready
      return self.clients.openWindow('/').then(newClient => {
        if (newClient && text) {
          // Give page a moment to load its SW message listener
          setTimeout(() => {
            newClient.postMessage({ type: 'SPEAK_REMINDER', text, lang: lang || 'en' });
          }, 1500);
        }
      });
    })
  );
});