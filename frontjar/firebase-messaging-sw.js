// firebase-messaging-sw.js — JarAI FCM Service Worker
// IMPORTANT: This file must be at the root scope ("/firebase-messaging-sw.js")

importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-messaging-compat.js');

firebase.initializeApp({
  apiKey:            "AIzaSyC4wy6kNsI7IE7OeWNUO9pNcVSM-Zp1rcE",
  authDomain:        "jarai-app.firebaseapp.com",
  projectId:         "jarai-app",
  storageBucket:     "jarai-app.firebasestorage.app",
  messagingSenderId: "221977713173",
  appId:             "1:221977713173:web:f7d934e9b1b2eba512cc1c",
});

const messaging = firebase.messaging();

// ── Background message handler ─────────────────────────────────────────────
// Triggered when FCM message arrives while app is in background/closed.
// main.py sends: notification={title,body} + data={id, lang, body}
//
// The "undefined" bug was because payload.notification.body was empty
// and payload.data.body wasn't being read.
// Fix: always prefer data.body, fall back to notification.body.

messaging.onBackgroundMessage(payload => {
  console.log('[FCM-SW] Background message:', JSON.stringify(payload));

  // Read from data fields first (set explicitly in main.py send_fcm())
  const title = payload.data?.title
    || payload.notification?.title
    || '⏰ Reminder';

  const body  = payload.data?.body         // ← THIS is where your text lives
    || payload.notification?.body
    || '';                                  // Never show "undefined"

  const lang  = payload.data?.lang  || 'en';
  const id    = payload.data?.id    || '';

  if (!body) {
    console.warn('[FCM-SW] Empty body — skipping notification display');
    return;
  }

  return self.registration.showNotification(title, {
    body,
    icon:               '/icons/icon-192.png',
    badge:              '/icons/icon-192.png',
    tag:                id || 'jarai-fcm',
    requireInteraction: true,              // stays until tapped (Android key)
    vibrate:            [200, 100, 200, 100, 400],
    data:               { text: body, lang, id },
  });
});

// ── Notification click ─────────────────────────────────────────────────────
// Tapping the FCM notification → open/focus app → speak reminder text
self.addEventListener('notificationclick', e => {
  e.notification.close();

  const { text, lang } = e.notification.data || {};

  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      const postSpeak = (client) => {
        if (text) client.postMessage({ type: 'SPEAK_REMINDER', text, lang: lang || 'en' });
      };

      for (const client of clientList) {
        if ('focus' in client) {
          client.focus();
          postSpeak(client);
          return;
        }
      }

      return self.clients.openWindow('/').then(newClient => {
        if (newClient && text) {
          setTimeout(() => {
            newClient.postMessage({ type: 'SPEAK_REMINDER', text, lang: lang || 'en' });
          }, 1500);
        }
      });
    })
  );
});