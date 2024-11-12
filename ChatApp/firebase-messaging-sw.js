// firebase-messaging-sw.js
// Hi :)
// Firebase Messaging Service Worker
importScripts('https://cdnjs.cloudflare.com/ajax/libs/firebase/9.12.1/firebase-app-compat.js');
importScripts('https://cdnjs.cloudflare.com/ajax/libs/firebase/9.12.1/firebase-messaging-compat.js');

// Your Firebase configuration object
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

// Initialize Firebase Cloud Messaging
const messaging = firebase.messaging();

// Handle background messages
messaging.onBackgroundMessage((payload) => {
    console.log('Received background message:', payload);

    const notificationTitle = payload.notification.title;
    const notificationOptions = {
        body: payload.notification.body,
        icon: '/static/images/chat-icon.png', // Replace with path to your notification icon
        badge: '/static/images/recent-chats-icon.png', // Replace with path to your notification badge
        data: payload.data,
        actions: [
            {
                action: 'open_chat',
                title: 'Open Chat'
            }
        ]
    };

    return self.registration.showNotification(notificationTitle, notificationOptions);
});

// Handle notification click
self.addEventListener('notificationclick', (event) => {
    console.log('Notification clicked:', event);

    event.notification.close();

    // This looks to see if the current is already open and focuses if it is
    event.waitUntil(
        clients.matchAll({
            type: "window",
            includeUncontrolled: true
        })
        .then((clientList) => {
            for (const client of clientList) {
                if (client.url && 'focus' in client) {
                    return client.focus();
                }
            }
            // If no window is already open, open a new one
            if (clients.openWindow) {
                return clients.openWindow('/');
            }
        })
    );
});

// Handle push events
self.addEventListener('push', (event) => {
    console.log('Push received:', event);

    if (event.data) {
        const data = event.data.json();
        const options = {
            body: data.notification.body,
            icon: '/static/images/chat-icon.png', // Replace with path to your notification icon
            badge: '/static/images/recent-chats-icon.png', // Replace with path to your notification badge
            data: data.data,
            actions: [
                {
                    action: 'open_chat',
                    title: 'Open Chat'
                }
            ]
        };

        event.waitUntil(
            self.registration.showNotification(data.notification.title, options)
        );
    }
});
// Add the photo caches here