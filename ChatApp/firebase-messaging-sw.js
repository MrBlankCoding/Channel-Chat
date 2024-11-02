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

// Platform detection (can be used for platform-specific handling)
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
const isAndroid = /Android/.test(navigator.userAgent);

// Handle background messages
messaging.onBackgroundMessage((payload) => {
  console.log('Received background message:', payload);

  // Extract notification data
  const notificationData = payload.data || {};
  const notification = payload.notification || {};

  // Platform-specific notification options
  const platformSpecificOptions = {
    // Android-specific options
    ...(isAndroid && {
      icon: notification.icon || '/android-icon.png',
      badge: '/android-badge.png',
      vibrate: [200, 100, 200], // Vibration pattern
      actions: [
        {
          action: 'view',
          title: 'View',
          icon: '/view-icon.png' // Android supports action icons
        },
        {
          action: 'close',
          title: 'Dismiss',
          icon: '/close-icon.png'
        }
      ],
      // Android notification channel
      tag: notificationData.type || 'default', // Group similar notifications
      renotify: true, // Notify even if using the same tag
    }),

    // iOS-specific options
    ...(isIOS && {
      icon: notification.icon || '/ios-icon.png',
      badge: '/ios-badge.png',
      // iOS doesn't support custom actions in PWAs, so we'll use basic options
      actions: [
        {
          action: 'view',
          title: 'View'
        }
      ],
      // Sound is handled through the payload for iOS
      sound: notification.sound || 'default'
    })
  };

  // Merge notification options with platform-specific options
  const notificationOptions = {
    body: notification.body || notificationData.body || 'New message received',
    icon: notification.icon || '/icon.png', // Fallback icon
    badge: '/badge.png',
    timestamp: Date.now(),
    requireInteraction: true,
    silent: false, // Enable sound (if supported by platform)
    data: {
      click_action: notification.click_action || notificationData.click_action || '/',
      messageId: notificationData.message_id,
      roomId: notificationData.room_id,
      url: notificationData.url || self.location.origin,
      ...notificationData,
      // Add platform info for handling clicks
      platform: isIOS ? 'ios' : (isAndroid ? 'android' : 'other')
    },
    ...platformSpecificOptions
  };

  // Show the notification with platform-specific options
  return self.registration.showNotification(
    notification.title || 'New Message',
    notificationOptions
  );
});

// Handle notification clicks with platform-specific behavior
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const notificationData = event.notification.data;
  const action = event.action;
  const platform = notificationData.platform;

  // Platform-specific click handling
  const handleClick = () => {
    const urlToOpen = new URL(notificationData.click_action || '/', self.location.origin).href;

    return clients.matchAll({
      type: 'window',
      includeUncontrolled: true
    })
    .then((windowClients) => {
      // iOS specific: Always open new window due to PWA limitations
      if (platform === 'ios') {
        return clients.openWindow(urlToOpen);
      }

      // Android and others: Try to focus existing window first
      for (const client of windowClients) {
        if (client.url === urlToOpen) {
          return client.focus();
        }
      }
      return clients.openWindow(urlToOpen);
    });
  };

  if (action === 'view' || !action) {
    event.waitUntil(handleClick());
  }
});

// Enhanced cache strategy for notification assets
workbox.routing.registerRoute(
  ({request}) => request.destination === 'image',
  new workbox.strategies.CacheFirst({
    cacheName: 'notification-images',
    plugins: [
      new workbox.expiration.ExpirationPlugin({
        maxEntries: 60,
        maxAgeSeconds: 30 * 24 * 60 * 60, // 30 days
      }),
      new workbox.cacheableResponse.CacheableResponsePlugin({
        statuses: [0, 200] // Cache successful responses and opaque responses
      })
    ],
  })
);

// Error handling with platform information
self.addEventListener('error', (event) => {
  console.error('Service Worker error:', {
    error: event.error,
    platform: isIOS ? 'ios' : (isAndroid ? 'android' : 'other')
  });
});

// Service worker activation with platform-specific cleanup
self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      // Clear old caches
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