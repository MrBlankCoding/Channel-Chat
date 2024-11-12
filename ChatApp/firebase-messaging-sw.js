// firebase-messaging-sw.js
importScripts('https://www.gstatic.com/firebasejs/10.14.1/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.14.1/firebase-messaging-compat.js');

firebase.initializeApp({
    apiKey: "AIzaSyCjJzQGCZ0niMD5tek_0gLSBGJXxW0VLKA",
    authDomain: "channelchat-7d679.firebaseapp.com",
    projectId: "channelchat-7d679",
    storageBucket: "channelchat-7d679.appspot.com",
    messagingSenderId: "822894243205",
    appId: "1:822894243205:web:8c8b1648fece9ae33e68ec"
});

const messaging = firebase.messaging();

// Check if any window clients are focused
async function isAppActive() {
    const windowClients = await clients.matchAll({
        type: 'window',
        includeUncontrolled: true
    });
    return windowClients.some(client => client.focused);
}

// Handle background messages
messaging.onBackgroundMessage(async (payload) => {
    // Check if app is active
    const appIsActive = await isAppActive();
    
    if (appIsActive) {
        // If app is active, forward the message to the client
        const windowClients = await clients.matchAll({
            type: 'window',
            includeUncontrolled: true
        });
        
        windowClients.forEach(client => {
            client.postMessage({
                type: 'notification',
                payload
            });
        });
        
        // Don't show the notification if app is active
        return;
    }
    
    // If app is not active, show the notification
    const { title, body, icon } = payload.notification;
    
    const options = {
        body,
        icon: icon || '/path/to/icon.png',
        badge: '/path/to/badge.png',
        vibrate: [200, 100, 200],
        tag: 'message-notification'
    };

    return self.registration.showNotification(title, options);
});

// Handle notification clicks
self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    
    // Focus on existing window or open a new one
    event.waitUntil(
        clients.matchAll({ type: 'window' }).then(clientList => {
            for (const client of clientList) {
                if (client.url && 'focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow('/');
            }
        })
    );
});