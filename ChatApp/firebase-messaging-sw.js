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