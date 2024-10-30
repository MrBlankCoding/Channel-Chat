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
  appId: "1:822894243205:web:e129bcac94601e183e68ec"
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
    // Open the specific chat or message
    const urlToOpen = new URL(notificationData.click_action || '/', self.location.origin).href;

    event.waitUntil(
      clients.matchAll({
        type: 'window',
        includeUncontrolled: true
      })
      .then((windowClients) => {
        // Check if there's already a window open
        for (const client of windowClients) {
          if (client.url === urlToOpen) {
            return client.focus();
          }
        }
        // If no window is open, open a new one
        return clients.openWindow(urlToOpen);
      })
    );
  }
  // 'close' action is handled automatically by closing the notification
});

// Handle notification close events
self.addEventListener('notificationclose', (event) => {
  const dismissedNotification = event.notification;
  const notificationData = dismissedNotification.data;
  
  // You could send analytics data here if needed
  console.log('Notification was dismissed', notificationData);
});

// Cache strategy for notification assets
workbox.routing.registerRoute(
  ({request}) => request.destination === 'image',
  new workbox.strategies.CacheFirst({
    cacheName: 'notification-images',
    plugins: [
      new workbox.expiration.ExpirationPlugin({
        maxEntries: 60,
        maxAgeSeconds: 30 * 24 * 60 * 60, // 30 days
      }),
    ],
  })
);

// Handle errors
self.addEventListener('error', (event) => {
  console.error('Service Worker error:', event.error);
});

// Optional: Handle service worker updates
self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      // Clear old caches if needed
      caches.keys().then(cacheNames => {
        return Promise.all(
          cacheNames.map(cache => {
            if (cache !== 'notification-images') {
              return caches.delete(cache);
            }
          })
        );
      }),
      // Claim clients
      clients.claim()
    ])
  );
});