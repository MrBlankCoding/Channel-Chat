import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import { getMessaging, getToken, onMessage } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-messaging.js";

class NotificationManager {
    constructor(firebaseConfig) {
        this.app = initializeApp(firebaseConfig);
        this.messaging = getMessaging(this.app);
        this.notificationButton = document.getElementById("toggle-notifications");
        
        this.settings = {
            enabled: false,
            direct_messages: true,
            mentions: true,
            group_messages: true,
            sound_enabled: true,
            vibration_enabled: true
        };

        this.initialize();
    }

    async initialize() {
        try {
            // Load current settings from backend
            const response = await fetch('/notification-settings');
            if (response.ok) {
                this.settings = await response.json();
                this.updateNotificationButton(this.settings.enabled);
            }

            // Setup notification button
            this.setupNotificationButton();
            
            // Setup message handler
            this.setupMessageHandler();

        } catch (error) {
            console.error('Initialization error:', error);
        }
    }

    setupMessageHandler() {
        onMessage(this.messaging, (payload) => {
            const { title, body } = payload.notification;
            const notificationOptions = {
                body,
                icon: '/path/to/icon.png', // Update with your icon path
                vibrate: this.settings.vibration_enabled ? [200, 100, 200] : undefined,
                silent: !this.settings.sound_enabled
            };

            new Notification(title, notificationOptions);
        });
    }

    setupNotificationButton() {
        this.notificationButton.addEventListener("click", async () => {
            try {
                if (this.settings.enabled) {
                    await this.disableNotifications();
                } else {
                    await this.enableNotifications();
                }
            } catch (error) {
                console.error('Error toggling notifications:', error);
                alert('Failed to update notification settings. Please try again.');
            }
        });
    }

    async enableNotifications() {
        try {
            const permission = await Notification.requestPermission();
            if (permission !== 'granted') {
                throw new Error('Notification permission denied');
            }
    
            // Register service worker if not already registered
            if ('serviceWorker' in navigator) {
                const registration = await navigator.serviceWorker.register('/firebase-messaging-sw.js', {
                    scope: '/'
                });
    
                // Wait for the service worker to be ready
                await navigator.serviceWorker.ready;
                console.log("Registered again :)");
    
                const token = await getToken(this.messaging, {
                    vapidKey: 'BFF7GvyyBdEKRjSCNeDKIB0U85iGp7-wUm-mtV7GmBgq5FHqsQmcxTWe9QElWuwhdZEZ6xhGMcCRSreeAq-XSlE',
                    serviceWorkerRegistration: registration
                });
    
                if (!token) {
                    throw new Error('No registration token available');
                }
    
                // Register token with backend
                const response = await fetch('/register-fcm-token', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token })
                });
    
                if (!response.ok) {
                    throw new Error('Failed to register token with server');
                }
    
                // Update settings
                this.settings.enabled = true;
                await this.updateSettings(this.settings);
                this.updateNotificationButton(true);
    
            }
        } catch (error) {
            console.error('Error enabling notifications:', error);
            throw error;
        }
    }

    async disableNotifications() {
        try {
            // Clear token from backend
            const response = await fetch('/register-fcm-token', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clearAll: true })
            });

            if (!response.ok) {
                throw new Error('Failed to clear token from server');
            }

            // Update settings
            this.settings.enabled = false;
            await this.updateSettings(this.settings);
            this.updateNotificationButton(false);

        } catch (error) {
            console.error('Error disabling notifications:', error);
            throw error;
        }
    }

    async updateSettings(settings) {
        const response = await fetch('/notification-settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(settings)
        });

        if (!response.ok) {
            throw new Error('Failed to update notification settings');
        }

        this.settings = settings;
    }

    updateNotificationButton(isEnabled) {
        const icon = isEnabled ? this.notificationIcons.enabled : this.notificationIcons.disabled;
        
        this.notificationButton.innerHTML = icon;
        this.notificationButton.style.border = 'none';
        this.notificationButton.style.padding = '8px';
        this.notificationButton.style.borderRadius = '50%';
        this.notificationButton.style.cursor = 'pointer';
    }

    notificationIcons = {
        enabled: `<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 mr-2" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
            <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
            <path d="M12 2v1"></path>
        </svg>`,
        disabled: `<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 mr-2" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
            <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
            <line x1="2" y1="2" x2="22" y2="22"></line>
        </svg>`
    };
}

// Initialize the notification manager
document.addEventListener("DOMContentLoaded", () => {
    const firebaseConfig = {
        apiKey: "AIzaSyCjJzQGCZ0niMD5tek_0gLSBGJXxW0VLKA",
        authDomain: "channelchat-7d679.firebaseapp.com",
        projectId: "channelchat-7d679",
        storageBucket: "channelchat-7d679.appspot.com",
        messagingSenderId: "822894243205",
        appId: "1:822894243205:web:8c8b1648fece9ae33e68ec",
        measurementId: "G-W4NJ28ZYC6"
      };

    const notificationManager = new NotificationManager(firebaseConfig);
});