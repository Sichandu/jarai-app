// Firebase Cloud Messaging Service Worker
// This runs at OS level — wakes up even when browser is closed on Android

importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-messaging-compat.js');

// REPLACE these with your actual Firebase config values
firebase.initializeApp({
  apiKey: "AIzaSyC4wy6kNsI7IE7OeWNUO9pNcVSM-Zp1rcE",
  authDomain: "jarai-app.firebaseapp.com",
  projectId: "jarai-app",
  storageBucket: "jarai-app.firebasestorage.app",
  messagingSenderId: "221977713173",
  appId: "1:221977713173:web:f7d934e9b1b2eba512cc1c",
});

const messaging = firebase.messaging();

// Handle background messages (app closed / in background)
messaging.onBackgroundMessage(function(payload) {
  console.log('[FCM SW] Background message received:', payload);
  const { title, body, lang } = payload.data || {};
  const notifTitle = title || '⏰ Reminder';
  const notifBody  = body  || payload.notification?.body || '';

  self.registration.showNotification(notifTitle, {
    body:             notifBody,
    icon:             '/icons/icon-192.png',
    badge:            '/icons/icon-96.png',
    vibrate:          [200, 100, 200, 100, 200],
    requireInteraction: true,
    data:             { lang, text: notifBody },
  });
});

// Notification click — open app and speak
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      const existing = list.find(c => c.url.includes(self.location.origin));
      if (existing) {
        existing.focus();
        existing.postMessage({
          type: 'SPEAK_REMINDER',
          text: event.notification.data?.text,
          lang: event.notification.data?.lang,
        });
      } else {
        clients.openWindow('/');
      }
    })
  );
});