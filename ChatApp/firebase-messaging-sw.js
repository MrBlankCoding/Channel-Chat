// Import necessary Firebase and Workbox scripts
importScripts('https://www.gstatic.com/firebasejs/10.14.1/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.14.1/firebase-messaging-compat.js');
importScripts('https://storage.googleapis.com/workbox-cdn/releases/6.4.1/workbox-sw.js');

// Firebase configuration
const firebaseConfig = {
  apiKey: "AIzaSyCjJzQGCZ0niMD5tek_0gLSBGJXxW0VLKA",
  authDomain: "channelchat-7d679.firebaseapp.com",
  projectId: "channelchat-7d679",
  storageBucket: "channelchat-7d679.appspot.com",
  messagingSenderId: "822894243205",
  appId: "1:822894243205:web:8c8b1648fece9ae33e68ec",
  measurementId: "G-W4NJ28ZYC6"
};

// Initialize Firebase
firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

// Handle background messages
messaging.onBackgroundMessage((payload) => {
  console.log('Received background message:', payload);

  // Extract notification data
  const notificationData = payload.data || {};
  const notification = payload.notification || {};

  // Merge notification options with defaults
  const notificationOptions = {
    body: notification.body || notificationData.body || 'New message received',
    icon: notification.icon || '/icon.png',
    badge: '/badge.png',
    timestamp: Date.now(),
    requireInteraction: true,
    data: {
      click_action: notification.click_action || notificationData.click_action || '/',
      messageId: notificationData.message_id,
      roomId: notificationData.room_id,
      url: notificationData.url || window.location.origin,
      ...notificationData
    },
    actions: [
      {
        action: 'view',
        title: 'View'
      },
      {
        action: 'close',
        title: 'Dismiss'
      }
    ]
  };

  // Show the notification
  return self.registration.showNotification(
    notification.title || 'New Message',
    notificationOptions
  );
});

// Handle notification clicks
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const notificationData = event.notification.data;
  const action = event.action;

  // Handle different actions
  if (action === 'view') {
    const urlToOpen = new URL(notificationData.click_action || '/', self.location.origin).href;

    event.waitUntil(
      clients.matchAll({
        type: 'window',
        includeUncontrolled: true
      })
      .then((windowClients) => {
        for (const client of windowClients) {
          if (client.url === urlToOpen) {
            return client.focus();
          }
        }
        return clients.openWindow(urlToOpen);
      })
    );
  }
});

// Handle notification close events
self.addEventListener('notificationclose', (event) => {
  const dismissedNotification = event.notification;
  const notificationData = dismissedNotification.data;
  console.log('Notification was dismissed', notificationData);
});

// Remove notification images cache and clear all caches on activate
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cache) => caches.delete(cache))
      );
    }).then(() => clients.claim())
  );
});