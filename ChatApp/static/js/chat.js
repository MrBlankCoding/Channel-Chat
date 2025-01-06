// Import the Firebase SDK
import {
    initializeApp
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import {
    getStorage,
    ref,
    uploadBytesResumable,
    getDownloadURL,
    deleteObject
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js';

// Your Firebase configuration (replace with your actual values)
const firebaseConfig = {
    apiKey: "AIzaSyCjJzQGCZ0niMD5tek_0gLSBGJXxW0VLKA",
    authDomain: "channelchat-7d679.firebaseapp.com",
    projectId: "channelchat-7d679",
    storageBucket: "channelchat-7d679.appspot.com",
    messagingSenderId: "822894243205",
    appId: "1:822894243205:web:8c8b1648fece9ae33e68ec",
    measurementId: "G-W4NJ28ZYC6"
};

const LINKPREVIEW_API_KEY = '1c04df7c16f6df68d9c4d8fb66c68a2e';
const LINKPREVIEW_API_URL = 'https://api.linkpreview.net/';

// Initialize Firebase
const app = initializeApp(firebaseConfig);

// Get a reference to the storage service
const storage = getStorage(app);

// Constants and DOM elements
const TYPING_TIMEOUT = 1000;
const messages = document.getElementById("messages");
const messageInput = document.getElementById("message");
const fileUpload = document.getElementById('image-upload');
const username = document.getElementById("username").value;
const unreadMessages = new Set();
const originalTitle = document.title;
const COMMON_EMOJIS = ['👍', '❤️', '😊', '😂'];

// State variables
let replyingTo = null;
let typingTimeout;
let currentUser = null;
let typingUsers = new Set();
let lastReadMessageId = null;
let isTabActive = true;
let unreadCount = 0;
let hasMoreMessages = false;
let isLoadingMessages = false;
let oldestMessageId = null;

//Local Storage
const LS_KEYS = {
    UNREAD_COUNT: 'unreadCount',
    LAST_READ_MESSAGE_ID: 'lastReadMessageId',
    USERNAME: 'username',
};

var socketio = io({
    transports: ['websocket'] // Ensure only WebSocket is used
});

// Helper functions

const fetchLinkPreview = async (url) => {
    try {
        const apiUrl = new URL(LINKPREVIEW_API_URL);
        apiUrl.searchParams.append('key', LINKPREVIEW_API_KEY);
        apiUrl.searchParams.append('q', url);

        const response = await fetch(apiUrl.toString(), {
            method: 'GET',
            headers: {
                'Content-Type': 'application/json',
            }
        });

        if (!response.ok) {
            throw new Error(`Failed to fetch link preview: ${response.status}`);
        }

        const data = await response.json();
        return {
            url: data.url,
            title: data.title,
            image: data.image ? new URL(data.image).protocol === 'http:' ?
                data.image.replace('http:', 'https:') :
                data.image : null,
            favicon: data.favicon ?
                new URL(data.favicon).protocol === 'http:' ?
                data.favicon.replace('http:', 'https:') :
                data.favicon : `https://www.google.com/s2/favicons?domain=${new URL(data.url).hostname}`
        };
    } catch (error) {
        console.error('Error fetching link preview:', error);
        return null;
    }
};

// Update the message content creation part in createMessageElement
const updateMessageContentWithLinks = (msg, isCurrentUser) => {
    const messageContent = document.createElement("div");
    messageContent.className = "message-content break-words";

    const urlRegex = /(https?:\/\/[^\s]+)/g;
    const urls = msg.match(urlRegex);

    if (urls) {
        // Check if the message contains only a URL
        const strippedMsg = msg.trim();
        const isOnlyUrl = urls.length === 1 && urls[0] === strippedMsg;

        if (isOnlyUrl) {
            // Create preview container that takes up full width
            const previewsContainer = document.createElement('div');
            previewsContainer.className = 'space-y-2';

            // Add loading state
            const loadingPreview = createLoadingPreview();
            previewsContainer.appendChild(loadingPreview);

            // Fetch and replace with actual preview
            fetchLinkPreview(urls[0])
                .then(metadata => {
                    if (metadata) {
                        const preview = createLinkPreview(metadata, isCurrentUser);
                        loadingPreview.replaceWith(preview);
                    } else {
                        loadingPreview.remove();
                        messageContent.textContent = msg; // Fallback to showing the URL
                    }
                })
                .catch(() => {
                    loadingPreview.remove();
                    messageContent.textContent = msg; // Fallback to showing the URL
                });

            return {
                messageContent,
                previewsContainer
            };
        } else {
            // If message contains other text besides URLs, handle normally
            let lastIndex = 0;
            const fragments = [];

            urls.forEach(url => {
                const index = msg.indexOf(url, lastIndex);
                if (index > lastIndex) {
                    fragments.push(document.createTextNode(msg.substring(lastIndex, index)));
                }

                const link = document.createElement('a');
                link.href = url;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.className = `${isCurrentUser ? 'text-blue-200' : 'text-blue-500'} hover:underline`;
                link.textContent = url;
                fragments.push(link);

                lastIndex = index + url.length;
            });

            if (lastIndex < msg.length) {
                fragments.push(document.createTextNode(msg.substring(lastIndex)));
            }

            fragments.forEach(fragment => messageContent.appendChild(fragment));
        }
    } else {
        messageContent.textContent = msg;
    }

    return {
        messageContent
    };
};

const createLinkPreview = (metadata, isCurrentUser) => {
    const previewContainer = document.createElement('div');
    previewContainer.className = `w-full rounded-lg overflow-hidden bg-white dark:bg-gray-800 shadow-sm hover:shadow-md transition-shadow duration-200 ${
          isCurrentUser ? 'border-blue-400/20' : 'border-gray-200 dark:border-gray-700'
      } border`;

    const content = document.createElement('a');
    content.href = metadata.url;
    content.target = '_blank';
    content.rel = 'noopener noreferrer';
    content.className = 'block';

    // Thumbnail section
    if (metadata.image) {
        const thumbnailContainer = document.createElement('div');
        thumbnailContainer.className = 'relative w-full aspect-[1.91/1] bg-gray-100 dark:bg-gray-700';

        const img = document.createElement('img');
        img.src = metadata.image;
        img.alt = metadata.title || 'Link preview';
        img.className = 'w-full h-full object-cover transition-opacity duration-200';
        img.style.opacity = '0';

        // Add loading state
        const loadingOverlay = document.createElement('div');
        loadingOverlay.className = 'absolute inset-0 flex items-center justify-center bg-gray-100 dark:bg-gray-700';
        loadingOverlay.innerHTML = `
              <svg class="animate-spin h-6 w-6 text-gray-400" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
          `;

        img.onload = () => {
            img.style.opacity = '1';
            loadingOverlay.remove();
        };

        img.onerror = () => {
            thumbnailContainer.remove();
        };

        thumbnailContainer.appendChild(loadingOverlay);
        thumbnailContainer.appendChild(img);
        content.appendChild(thumbnailContainer);
    }

    // Text content section
    const textContent = document.createElement('div');
    textContent.className = 'p-4 space-y-2';

    if (metadata.title) {
        const title = document.createElement('h3');
        title.className = `text-sm font-medium ${
              isCurrentUser ? 'text-white' : 'text-gray-900 dark:text-white'
          } line-clamp-2`;
        title.textContent = metadata.title;
        textContent.appendChild(title);
    }

    // Domain section with favicon
    const domainInfo = document.createElement('div');
    domainInfo.className = `flex items-center gap-1.5 text-xs ${
          isCurrentUser ? 'text-blue-200' : 'text-gray-500 dark:text-gray-400'
      }`;

    const favicon = document.createElement('img');
    favicon.src = metadata.favicon;
    favicon.className = 'w-4 h-4';
    favicon.alt = '';

    const domain = document.createElement('span');
    domain.textContent = new URL(metadata.url).hostname;

    domainInfo.appendChild(favicon);
    domainInfo.appendChild(domain);
    textContent.appendChild(domainInfo);

    content.appendChild(textContent);
    previewContainer.appendChild(content);

    return previewContainer;
};

const createLoadingPreview = () => {
    const loadingPreview = document.createElement('div');
    loadingPreview.className = 'w-full border dark:border-gray-700 rounded-lg overflow-hidden bg-white dark:bg-gray-800 animate-pulse';

    const loadingContent = document.createElement('div');
    loadingContent.className = 'space-y-3';

    // Loading thumbnail
    const loadingThumbnail = document.createElement('div');
    loadingThumbnail.className = 'w-full aspect-[1.91/1] bg-gray-200 dark:bg-gray-700';
    loadingContent.appendChild(loadingThumbnail);

    // Loading text content
    const textContent = document.createElement('div');
    textContent.className = 'p-4 space-y-2';

    // Loading title
    const loadingTitle = document.createElement('div');
    loadingTitle.className = 'h-4 bg-gray-200 dark:bg-gray-700 rounded w-3/4';
    textContent.appendChild(loadingTitle);

    // Loading domain
    const loadingDomain = document.createElement('div');
    loadingDomain.className = 'mt-2 flex items-center gap-1.5';

    const loadingFavicon = document.createElement('div');
    loadingFavicon.className = 'w-4 h-4 bg-gray-200 dark:bg-gray-700 rounded-full';

    const loadingDomainText = document.createElement('div');
    loadingDomainText.className = 'h-3 bg-gray-200 dark:bg-gray-700 rounded w-24';

    loadingDomain.appendChild(loadingFavicon);
    loadingDomain.appendChild(loadingDomainText);
    textContent.appendChild(loadingDomain);

    loadingContent.appendChild(textContent);
    loadingPreview.appendChild(loadingContent);

    return loadingPreview;
};

const updatePageTitle = () => {
    if (unreadCount > 0) {
        document.title = `(${unreadCount}) ${originalTitle}`;
    } else {
        document.title = originalTitle;
    }
    updateLocalStorage(LS_KEYS.UNREAD_COUNT, unreadCount.toString());
};

const handleVisibilityChange = () => {
    if (document.hidden) {
        isTabActive = false;
    } else {
        isTabActive = true;
        if (unreadCount > 0) {
            markMessagesAsRead();
            unreadCount = 0;
            updatePageTitle();
        }
    }
};

// Save data to Local Storage
const saveToLocalStorage = () => {
    localStorage.setItem(LS_KEYS.UNREAD_COUNT, unreadCount.toString());
    localStorage.setItem(LS_KEYS.LAST_READ_MESSAGE_ID, lastReadMessageId);
    localStorage.setItem(LS_KEYS.USERNAME, currentUser);
};

// Update specific items in Local Storage
const updateLocalStorage = (key, value) => {
    localStorage.setItem(key, value);
};

document.addEventListener("visibilitychange", handleVisibilityChange);

// Responsive message spacing variables
let MESSAGE = {
    padding: {
        vertical: 4, // Controls space between messages
        horizontal: 16, // Side padding of message container
        bubble: 6 // Padding inside message bubble
    },
    text: {
        size: 14, // Main message text size
        spacing: 2 // Space between elements (header to content)
    },
    maxWidth: { // Maximum width based on screen size
        default: '85%', // Default maximum width for mobile screens
        tablet: '70%', // Width for tablet-sized screens
        desktop: '60%', // Width for desktop screens
        largeDesktop: '50%' // Width for larger desktop screens
    },
    photo: {
        size: 24, // Profile photo dimensions
        gap: 8 // Space between photo and message
    }
};

// Function to get max width based on screen size
const getResponsiveMaxWidth = () => {
    const width = window.innerWidth;
    if (width < 768) return MESSAGE.maxWidth.default; // Mobile screens
    if (width < 1024) return MESSAGE.maxWidth.tablet; // Tablet screens
    if (width < 1440) return MESSAGE.maxWidth.desktop; // Small desktop screens
    return MESSAGE.maxWidth.largeDesktop; // Large desktop screens
};

const formatTimestamp = (timestamp) => {
    try {
        const userTimezone = localStorage.getItem('userTimezone') || 
            Intl.DateTimeFormat().resolvedOptions().timeZone;
        
        const date = new Date(timestamp);
        const now = new Date();
        const yesterday = new Date(now);
        yesterday.setDate(yesterday.getDate() - 1);
        
        if (date.toDateString() === now.toDateString()) {
            return date.toLocaleTimeString('default', {
                hour: 'numeric',
                minute: '2-digit',
                hour12: true,
                timeZone: userTimezone
            });
        } else if (date.toDateString() === yesterday.toDateString()) {
            return `Yesterday at ${date.toLocaleTimeString('default', {
                hour: 'numeric',
                minute: '2-digit',
                hour12: true,
                timeZone: userTimezone
            })}`;
        }
        
        return date.toLocaleString('default', {
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
            hour12: true,
            timeZone: userTimezone
        });
    } catch (e) {
        console.error('Error formatting timestamp:', e);
        return 'Invalid date';
    }
};

const createMessageElement = (name, msg, image, messageId, replyTo, isEdited = false, reactions = {}, readBy = [], type = 'normal', video = null, timestamp = '', gif = null) => {
    // Handle system messages differently
    if (type === 'system') {
        const element = document.createElement("div");
        element.className = 'message group py-2 flex justify-center';
        element.dataset.messageId = messageId;

        const systemMessage = document.createElement("div");
        systemMessage.className = 'px-4 py-1.5 bg-gray-100 dark:bg-gray-700 rounded-full inline-flex items-center gap-2';

        const iconSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        iconSvg.setAttribute("class", "w-3.5 h-3.5 text-gray-400 dark:text-gray-500");
        iconSvg.setAttribute("fill", "none");
        iconSvg.setAttribute("viewBox", "0 0 24 24");
        iconSvg.setAttribute("stroke", "currentColor");
        iconSvg.setAttribute("stroke-width", "2");

        const iconPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
        iconPath.setAttribute("stroke-linecap", "round");
        iconPath.setAttribute("stroke-linejoin", "round");
        iconPath.setAttribute("d", "M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z");

        iconSvg.appendChild(iconPath);
        systemMessage.appendChild(iconSvg);

        const messageText = document.createElement("span");
        messageText.className = 'text-sm text-gray-500 dark:text-gray-400';
        messageText.textContent = msg;

        systemMessage.appendChild(messageText);
        element.appendChild(systemMessage);

        return element;
    }

    const isCurrentUser = name === currentUser;
    const validReaders = readBy.filter(reader => reader !== null && reader !== name);
    const isRead = validReaders.length > 0;

    const element = document.createElement("div");
    element.style.padding = `${MESSAGE.padding.vertical}px ${MESSAGE.padding.horizontal}px`;
    element.style.display = 'flex';
    element.style.justifyContent = isCurrentUser ? 'flex-end' : 'flex-start';
    element.style.alignItems = 'flex-start';
    element.style.gap = `${MESSAGE.photo.gap}px`;
    element.dataset.messageId = messageId;
    element.className = 'message group hover:bg-gray-50/5 transition-colors duration-200 mb-6 relative';

    // Profile photo section
    if (!isCurrentUser) {
        const profilePhotoContainer = document.createElement("div");
        profilePhotoContainer.style.flexShrink = '0';
        const profilePhoto = document.createElement("img");
        profilePhoto.src = `/profile_photos/${name}`;
        profilePhoto.alt = `${name}'s profile`;
        profilePhoto.style.width = `${MESSAGE.photo.size}px`;
        profilePhoto.style.height = `${MESSAGE.photo.size}px`;
        profilePhoto.className = "rounded-full object-cover ring-1 ring-white dark:ring-gray-800 shadow-sm";
        profilePhoto.onerror = function() {
            this.src = '/static/images/default-profile.png';
        };
        profilePhotoContainer.appendChild(profilePhoto);
        element.appendChild(profilePhotoContainer);
    }

    const messageContainer = document.createElement("div");
    messageContainer.style.maxWidth = getResponsiveMaxWidth();
    messageContainer.className = "relative group";

    const timestampElement = document.createElement("div");
    timestampElement.className = "timestamp mt-1 text-xs text-gray-500 dark:text-gray-400 flex justify-end";

    // Align timestamp based on message sender
    if (!isCurrentUser) {
        timestampElement.style.justifyContent = 'flex-start';
    }

    // Format timestamp using the formatTimestamp function
    if (timestamp) {
        timestampElement.textContent = formatTimestamp(timestamp);
    }

    const messageHeader = document.createElement("div");
    messageHeader.style.display = 'flex';
    messageHeader.style.alignItems = 'center';
    messageHeader.style.gap = `${MESSAGE.text.spacing}px`;
    messageHeader.style.marginBottom = `${MESSAGE.text.spacing}px`;
    messageHeader.style.justifyContent = isCurrentUser ? 'flex-end' : 'flex-start';

    if (!isCurrentUser) {
        const nameSpan = document.createElement("span");
        nameSpan.className = "text-xs font-medium text-gray-700 dark:text-gray-300";
        nameSpan.textContent = name;
        messageHeader.appendChild(nameSpan);
    }

    messageContainer.appendChild(messageHeader);

    const messageBubble = document.createElement("div");
    messageBubble.style.padding = `${MESSAGE.padding.bubble}px`;
    messageBubble.style.fontSize = `${MESSAGE.text.size}px`;
    messageBubble.className = `relative ${
          isCurrentUser 
              ? 'bg-blue-500 text-white rounded-t-2xl rounded-l-2xl rounded-br-lg' 
              : 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white rounded-t-2xl rounded-r-2xl rounded-bl-lg'
      }`;

    if (isCurrentUser && isRead) {
        messageBubble.classList.add('bg-indigo-700');
    }

    // GIF handling
    if (gif && gif.url) {
        const gifContainer = document.createElement("div");
        gifContainer.className = "relative mt-2";

        const loadingPlaceholder = document.createElement("div");
        loadingPlaceholder.className = "animate-pulse bg-gray-200 dark:bg-gray-700 rounded-lg";
        loadingPlaceholder.style.width = "240px";
        loadingPlaceholder.style.height = "180px";
        gifContainer.appendChild(loadingPlaceholder);

        const gifElement = document.createElement("img");
        gifElement.className = "max-w-[240px] w-full rounded-lg shadow-sm hover:shadow-md transition-shadow duration-200 cursor-pointer";
        gifElement.loading = "lazy";
        gifElement.alt = gif.title || "GIF";
        
        gifContainer.appendChild(gifElement);

        gifElement.onload = () => {
            loadingPlaceholder.remove();
        };

        gifElement.onerror = (error) => {
            console.error('Failed to load GIF:', error);
            loadingPlaceholder.remove();
            const errorMessage = document.createElement("div");
            errorMessage.className = "text-sm text-red-500 dark:text-red-400 p-2 bg-red-100 dark:bg-red-900/20 rounded";
            errorMessage.textContent = "Failed to load GIF";
            gifContainer.appendChild(errorMessage);
            gifElement.remove();
        };

        gifElement.src = gif.url;

        gifElement.addEventListener("click", () => {
            const modal = document.createElement("div");
            modal.className = "fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-75 p-4";
            
            const modalContent = document.createElement("div");
            modalContent.className = "relative max-w-2xl w-full";
            
            const fullSizeGif = document.createElement("img");
            fullSizeGif.src = gif.url;
            fullSizeGif.alt = gif.title || "GIF";
            fullSizeGif.className = "w-full rounded-lg";
            
            const closeButton = document.createElement("button");
            closeButton.className = "absolute top-2 right-2 text-white hover:text-gray-300";
            closeButton.innerHTML = `
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                </svg>
            `;
            
            closeButton.onclick = () => modal.remove();
            modal.onclick = (e) => {
                if (e.target === modal) modal.remove();
            };
            
            modalContent.appendChild(fullSizeGif);
            modalContent.appendChild(closeButton);
            modal.appendChild(modalContent);
            document.body.appendChild(modal);
        });
        
        messageBubble.appendChild(gifContainer);
    }

    // Reply section
    if (replyTo) {
        const replyInfo = document.createElement("div");
        replyInfo.style.marginBottom = `${MESSAGE.text.spacing}px`;
        replyInfo.className = `text-xs rounded px-2 py-0.5 border-l-2 cursor-pointer hover:opacity-80 ${
              isCurrentUser
                  ? 'bg-blue-600/50 border-blue-300'
                  : 'bg-gray-200 dark:bg-gray-700 border-gray-400 dark:border-gray-500'
          }`;

        replyInfo.dataset.replyTo = replyTo.id;
        const replyMessage = replyTo.message || "Message not available";
        replyInfo.innerHTML = `<span class="opacity-75">Replying to:</span> ${replyMessage}`;
        replyInfo.addEventListener('click', () => scrollToMessage(replyTo.id));
        messageBubble.appendChild(replyInfo);
    }

    // Message content with URL detection and preview
    const {
        messageContent,
        previewsContainer
    } = updateMessageContentWithLinks(msg, isCurrentUser);
    messageBubble.appendChild(messageContent);

    if (previewsContainer) {
        messageBubble.appendChild(previewsContainer);
    }

    if (isEdited) {
        const editedIndicator = document.createElement("span");
        editedIndicator.className = "ml-1 text-xs opacity-75";
        editedIndicator.textContent = "(edited)";
        messageContent.appendChild(editedIndicator);
    }

    // Image handling
    if (image) {
        const img = document.createElement("img");
        img.src = image;
        img.alt = "Uploaded image";
        img.style.marginTop = `${MESSAGE.text.spacing}px`;
        img.className = "max-w-[120px] rounded-lg shadow-sm hover:shadow-md transition-shadow duration-200 opacity-50 sent-img";
        img.onload = () => img.classList.remove('opacity-50');
        img.onerror = () => {
            img.src = '/static/images/image-error.png';
            img.classList.remove('opacity-50');
        };
        messageBubble.appendChild(img);
        img.classList.add('cursor-pointer');
        img.addEventListener('click', () => openImageModal(image));
    }

    // Video handling
    if (video) {
        const videoContainer = document.createElement("div");
        videoContainer.className = "relative mt-2";

        const videoElement = document.createElement("video");
        videoElement.src = video;
        videoElement.className = "max-w-[240px] rounded-lg shadow-sm hover:shadow-md transition-shadow duration-200";
        videoElement.controls = true;
        videoElement.preload = "metadata";

        videoContainer.appendChild(videoElement);
        messageBubble.appendChild(videoContainer);
    }

    // Reactions
    const reactionsContainer = document.createElement("div");
    reactionsContainer.style.marginTop = `${MESSAGE.text.spacing}px`;
    reactionsContainer.className = "flex flex-wrap gap-0.5 reactions-container";

    updateReactionsDisplay(reactionsContainer, reactions, messageId);
    messageBubble.appendChild(reactionsContainer);

    // Actions menu
    const actionsMenu = createActionsMenu(isCurrentUser);
    messageBubble.appendChild(actionsMenu);

    messageContainer.appendChild(messageBubble);
    element.appendChild(messageContainer);

    addEventListeners(messageBubble, messageId, msg);

    messageContainer.appendChild(timestampElement);

    return element;
};

const createReactionElement = (emoji, {
    count,
    users
}, messageId) => {
    const button = document.createElement("button");
    const isUserReacted = users.includes(currentUser);

    button.className = `
    flex items-center gap-1 text-xs rounded-full px-2 py-0.5 transition-colors
    ${isUserReacted ? 'bg-gray-200 dark:bg-gray-700' : 'bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700'}
  `;

    button.innerHTML = `<span>${emoji}</span><span>${count}</span>`;
    button.dataset.emoji = emoji;
    button.dataset.messageId = messageId;

    button.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();

        // Add click feedback animation
        button.style.transform = 'scale(0.95)';
        setTimeout(() => {
            button.style.transform = 'scale(1)';
        }, 100);

        socketio.emit('toggle_reaction', {
            messageId,
            emoji
        });
    };

    return button;
};

const updateReactionsDisplay = (container, reactions, messageId) => {
    const currentReactions = container.querySelectorAll('button');
    const currentEmojis = new Set([...currentReactions].map(btn => btn.dataset.emoji));
    const newEmojis = new Set(Object.keys(reactions).filter(emoji => reactions[emoji].count > 0));

    // Handle removals with animation
    currentReactions.forEach(button => {
        const emoji = button.dataset.emoji;
        if (!newEmojis.has(emoji)) {
            // Animate out and remove
            button.style.transition = 'all 0.2s ease-out';
            button.style.transform = 'scale(0)';
            button.style.opacity = '0';
            setTimeout(() => button.remove(), 200);
        }
    });

    // Update existing reactions and add new ones
    Object.entries(reactions).forEach(([emoji, data]) => {
        if (data.count > 0) {
            const existingButton = container.querySelector(`button[data-emoji="${emoji}"]`);

            if (existingButton) {
                // Update existing reaction
                const countElement = existingButton.querySelector('span:last-child');
                if (countElement) {
                    // Animate count change
                    countElement.style.transition = 'transform 0.2s ease-out';
                    countElement.style.transform = 'scale(1.2)';
                    countElement.textContent = data.count;
                    setTimeout(() => {
                        countElement.style.transform = 'scale(1)';
                    }, 200);
                }

                // Update styling based on user reaction status
                const isUserReacted = data.users.includes(currentUser);
                existingButton.className = `
          flex items-center gap-1 text-xs rounded-full px-2 py-0.5 transition-colors
          ${isUserReacted ? 'bg-gray-200 dark:bg-gray-700' : 'bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700'}
        `;
            } else if (!currentEmojis.has(emoji)) {
                // Add new reaction with animation
                const reactionElement = createReactionElement(emoji, data, messageId);
                reactionElement.style.transform = 'scale(0)';
                reactionElement.style.opacity = '0';
                container.appendChild(reactionElement);

                // Trigger animation
                requestAnimationFrame(() => {
                    reactionElement.style.transition = 'all 0.2s ease-out';
                    reactionElement.style.transform = 'scale(1)';
                    reactionElement.style.opacity = '1';
                });
            }
        }
    });
};

const createTypingIndicator = (name) => {
    const element = document.createElement("div");
    element.style.padding = `${MESSAGE.padding.vertical}px ${MESSAGE.padding.horizontal}px`;
    element.style.display = 'flex';
    element.style.alignItems = 'flex-start';
    element.style.gap = `${MESSAGE.photo.gap}px`;
    element.className = 'typing-indicator group hover:bg-gray-50/5 transition-colors duration-200';

    // Profile photo section
    const profilePhotoContainer = document.createElement("div");
    profilePhotoContainer.style.flexShrink = '0';
    const profilePhoto = document.createElement("img");
    profilePhoto.src = `/profile_photos/${name}`;
    profilePhoto.alt = `${name}'s profile`;
    profilePhoto.style.width = `${MESSAGE.photo.size}px`;
    profilePhoto.style.height = `${MESSAGE.photo.size}px`;
    profilePhoto.className = "rounded-full object-cover ring-1 ring-white dark:ring-gray-800 shadow-sm";
    profilePhoto.onerror = function() {
        this.src = '/static/images/default-profile.png';
    };
    profilePhotoContainer.appendChild(profilePhoto);
    element.appendChild(profilePhotoContainer);

    // Message container
    const messageContainer = document.createElement("div");
    messageContainer.style.maxWidth = getResponsiveMaxWidth();
    messageContainer.className = "relative";

    // User name
    const nameSpan = document.createElement("span");
    nameSpan.className = "text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 block";
    nameSpan.textContent = name;
    messageContainer.appendChild(nameSpan);

    // Typing bubble
    const typingBubble = document.createElement("div");
    typingBubble.className = "bg-gray-100 dark:bg-gray-800 rounded-t-2xl rounded-r-2xl rounded-bl-lg p-4";
    typingBubble.style.width = "fit-content";

    // Dots container
    const dotsContainer = document.createElement("div");
    dotsContainer.className = "flex gap-1";

    // Create three dots with animation
    for (let i = 0; i < 3; i++) {
        const dot = document.createElement("div");
        dot.className = "w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500";
        dot.style.animation = `typingAnimation 1.4s infinite`;
        dot.style.animationDelay = `${i * 0.2}s`;
        dotsContainer.appendChild(dot);
    }

    typingBubble.appendChild(dotsContainer);
    messageContainer.appendChild(typingBubble);
    element.appendChild(messageContainer);

    return element;
};

document.getElementById('gifButton').addEventListener('click', () => {
    createGifPicker();
    // Close the plus menu after opening GIF picker
    document.getElementById('plusMenu').classList.add('hidden');
});

const createGifPicker = async () => {
    const modal = document.createElement("div");
    modal.className = "fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50";

    const content = document.createElement("div");
    content.className = "relative bg-white dark:bg-gray-800 rounded-lg p-6 w-full max-w-3xl max-h-[80vh] overflow-y-auto shadow-lg border border-gray-200 dark:border-gray-700";

    const closeButton = document.createElement("button");
    closeButton.className = "absolute top-4 right-4 text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-white focus:outline-none focus:ring focus:ring-gray-300 dark:focus:ring-gray-600";
    closeButton.innerHTML = "&times;";
    closeButton.setAttribute("aria-label", "Close modal");
    closeButton.setAttribute("tabindex", "0");
    closeButton.addEventListener("click", () => modal.remove());
    content.appendChild(closeButton);

    const header = document.createElement("h2");
    header.className = "text-lg font-semibold text-gray-800 dark:text-gray-100 mb-4 text-center";
    header.textContent = "Pick a GIF";
    content.appendChild(header);

    const searchInput = document.createElement("input");
    searchInput.type = "text";
    searchInput.placeholder = "Search GIFs...";
    searchInput.className = "w-full p-3 mb-4 border rounded-lg dark:bg-gray-700 dark:border-gray-600 focus:outline-none focus:ring focus:ring-gray-300 dark:focus:ring-gray-600";
    searchInput.setAttribute("aria-label", "Search GIFs");
    searchInput.setAttribute("tabindex", "0");

    const gifGrid = document.createElement("div");
    gifGrid.style.display = "grid";
    gifGrid.style.gridTemplateColumns = "repeat(4, 1fr)"; // 4x4 grid layout
    gifGrid.style.gap = "12px"; // Space between items
    gifGrid.style.width = "100%";
    gifGrid.style.boxSizing = "border-box"; // Include padding/borders in size

    const spinner = document.createElement("div");
    spinner.className = "w-8 h-8 border-4 border-t-transparent border-gray-400 rounded-full animate-spin mx-auto my-4";

    const footer = document.createElement("div");
    footer.className = "text-center mt-4 text-sm text-gray-500 dark:text-gray-400";
    footer.textContent = "Powered by GIF API";

    let searchTimeout;
    const itemsPerPage = 16;

    const searchGifs = async (query) => {
        try {
            gifGrid.innerHTML = ""; // Clear existing grid content
            gifGrid.appendChild(spinner); // Show spinner

            const response = await fetch(`/api/search-gifs?q=${encodeURIComponent(query)}&page=1&limit=${itemsPerPage}`);
            const data = await response.json();

            gifGrid.innerHTML = ""; // Remove spinner and populate grid

            if (data.results.length === 0) {
                gifGrid.innerHTML = `<div class="text-center text-gray-500 dark:text-gray-400">No GIFs found</div>`;
            } else {
                data.results.forEach((gif, index) => {
                    const gifContainer = document.createElement("div");
                    gifContainer.className = "relative group";

                    const gifElement = document.createElement("img");
                    gifElement.src = gif.media_formats.gif.url;
                    gifElement.className = "object-cover rounded transition-transform duration-200 hover:scale-105 w-full h-full";
                    gifElement.setAttribute("alt", gif.content_description || `GIF ${index + 1}`);
                    gifElement.setAttribute("tabindex", "0");

                    gifElement.addEventListener("click", () => {
                        const messageData = {
                            data: "",
                            gif: {
                                url: gif.media_formats.gif.url,
                                title: gif.content_description
                            },
                            replyTo: replyingTo ? {
                                id: replyingTo.id,
                                message: replyingTo.message
                            } : null
                        };

                        socketio.emit("message", messageData);
                        cancelReply(); // Clear any reply state
                        modal.remove();
                    });

                    const gifOverlay = document.createElement("div");
                    gifOverlay.className = "absolute inset-0 bg-black bg-opacity-20 opacity-0 group-hover:opacity-100 transition-opacity duration-200 rounded";

                    gifContainer.appendChild(gifElement);
                    gifContainer.appendChild(gifOverlay);
                    gifGrid.appendChild(gifContainer);
                });
            }
        } catch (error) {
            console.error("Error searching GIFs:", error);
        }
    };

    searchInput.addEventListener("input", (e) => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
            searchGifs(e.target.value);
        }, 300);
    });

    content.appendChild(searchInput);
    content.appendChild(gifGrid);
    content.appendChild(footer);
    modal.appendChild(content);

    // Close on background click
    modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.remove();
    });

    // Add keyboard event listener for closing the modal
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            modal.remove();
        }
    });

    document.body.appendChild(modal);

    // Load trending GIFs initially
    searchGifs("");
};


// Add CSS animation
const style = document.createElement('style');
style.textContent = `
  @keyframes typingAnimation {
    0%, 60%, 100% {
      transform: translateY(0);
      opacity: 0.4;
    }
    30% {
      transform: translateY(-4px);
      opacity: 1;
    }
  }
    .message .timestamp {
      opacity: 0;
      transition: opacity 0.2s ease-in-out;
  }
  
  .message:hover .timestamp {
      opacity: 1;
  }
  `;
document.head.appendChild(style);

// Modified typing indicator management
let typingIndicators = new Map(); // Map to store typing indicators by user

const updateTypingIndicators = () => {
    const typingContainer = document.getElementById('typing-indicators-container') || (() => {
        const container = document.createElement('div');
        container.id = 'typing-indicators-container';
        messages.parentNode.insertBefore(container, messages.nextSibling);
        return container;
    })();

    // Clear existing indicators for users who stopped typing
    for (const [user, indicator] of typingIndicators.entries()) {
        if (!typingUsers.has(user)) {
            indicator.remove();
            typingIndicators.delete(user);
        }
    }

    // Add indicators for new typing users
    for (const user of typingUsers) {
        if (!typingIndicators.has(user)) {
            const indicator = createTypingIndicator(user);
            typingIndicators.set(user, indicator);
            typingContainer.appendChild(indicator);
        }
    }
};

// Modified socket event handler
socketio.on("typing", (data) => {
    if (data.isTyping) {
        typingUsers.add(data.name);
    } else {
        typingUsers.delete(data.name);
    }
    updateTypingIndicators();
});

// Modified input event handler
messageInput.addEventListener("keyup", (event) => {
    if (event.key === "Enter") {
        sendMessage();
    } else {
        if (!typingTimeout) {
            socketio.emit("typing", {
                isTyping: true
            });
        }
        clearTimeout(typingTimeout);
        typingTimeout = setTimeout(() => {
            socketio.emit("typing", {
                isTyping: false
            });
            typingTimeout = null;
        }, TYPING_TIMEOUT);
    }
});

const sendMessage = () => {
    const message = messageInput.value.trim();
    const fileInput = document.getElementById('image-upload'); // Assuming 'fileUpload' for file input

    if (message || fileInput.files.length > 0) {
        if (fileInput.files.length > 0) {
            fileInput.dispatchEvent(new Event('change'));
        } else {
            const messageData = {
                data: message,
                replyTo: replyingTo ? {
                    id: replyingTo.id,
                    message: replyingTo.message
                } : null
            };
            socketio.emit("message", messageData);
        }

        messageInput.value = "";
        cancelReply();
        clearTimeout(typingTimeout);
        socketio.emit("typing", {
            isTyping: false
        });
    }
};


// Event listener to adjust max width on window resize
window.addEventListener('resize', () => {
    document.querySelectorAll('.message-container').forEach(container => {
        container.style.maxWidth = getResponsiveMaxWidth();
    });
});

const updateMessageReadStatus = (messageElement, isRead) => {
    if (messageElement) {
        const messageBubble = messageElement.querySelector('.message-content').parentElement;
        if (isRead) {
            messageBubble.classList.add('bg-indigo-800');
        } else {
            messageBubble.classList.remove('bg-indigo-700');
        }
    }
};

const createReactionPicker = (messageId) => {
    // Remove any existing picker with animation
    const existingPicker = document.querySelector('.reaction-picker');
    if (existingPicker) {
        existingPicker.style.opacity = '0';
        existingPicker.style.transform = 'translateX(-50%) translateY(2px)';
        setTimeout(() => existingPicker.remove(), 200);
    }

    const picker = document.createElement('div');
    picker.className = `
    reaction-picker absolute z-50 bg-white dark:bg-gray-900 
    rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 
    p-2 flex gap-1 opacity-0 transform translate-y-2
  `;

    picker.style.transition = 'all 0.2s ease-out';

    COMMON_EMOJIS.forEach(emoji => {
        const emojiButton = document.createElement('button');
        emojiButton.className = `
      w-8 h-8 flex items-center justify-center rounded hover:bg-gray-100 
      dark:hover:bg-gray-800 transition-all duration-200 text-lg
      transform hover:scale-110
    `;
        emojiButton.textContent = emoji;
        emojiButton.onclick = (e) => {
            e.preventDefault();
            e.stopPropagation();

            // Add click feedback
            emojiButton.style.transform = 'scale(0.9)';
            setTimeout(() => {
                socketio.emit('toggle_reaction', {
                    messageId,
                    emoji
                });
                picker.style.opacity = '0';
                picker.style.transform = 'translateX(-50%) translateY(2px)';
                setTimeout(() => picker.remove(), 200);
            }, 100);
        };
        picker.appendChild(emojiButton);
    });

    const message = document.querySelector(`[data-message-id="${messageId}"]`);
    if (!message) return;

    const messageBubble = message.querySelector('.message-content').closest('div');
    messageBubble.appendChild(picker);

    picker.style.bottom = '100%';
    picker.style.left = '50%';
    picker.style.transform = 'translateX(-50%) translateY(2px)';
    picker.style.marginBottom = '8px';

    requestAnimationFrame(() => {
        picker.style.opacity = '1';
        picker.style.transform = 'translateX(-50%) translateY(0)';
    });

    const handleClickOutside = (event) => {
        if (!picker.contains(event.target) && !event.target.closest('.action-btn[title="React"]')) {
            picker.style.opacity = '0';
            picker.style.transform = 'translateX(-50%) translateY(2px)';
            setTimeout(() => picker.remove(), 200);
            document.removeEventListener('click', handleClickOutside);
        }
    };

    setTimeout(() => {
        document.addEventListener('click', handleClickOutside);
    }, 0);

    return picker;
};

const styless = document.createElement('style');
styless.textContent = `
  .reaction-picker::after {
    content: '';
    position: absolute;
    bottom: -5px;
    left: 50%;
    transform: translateX(-50%) rotate(45deg);
    width: 10px;
    height: 10px;
    background: inherit;
    border-right: 1px solid;
    border-bottom: 1px solid;
    border-color: inherit;
  }
  `;
document.head.appendChild(styless);

const createActionsMenu = (isCurrentUser) => {
    const actionsMenu = document.createElement("div");
    actionsMenu.className = `actions-menu opacity-0 group-hover:opacity-100 absolute -top-8 ${
    isCurrentUser ? 'right-0' : 'left-0'
  } flex items-center space-x-1 bg-white dark:bg-gray-800 shadow-lg rounded-lg p-1 transition-all duration-200 z-10`;

    const actions = [{
            title: "React",
            icon: "M14.828 14.828a4 4 0 01-5.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z",
            className: "hover:bg-gray-100 dark:hover:bg-gray-700"
        },
        {
            title: "Reply",
            icon: "M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6",
            className: "hover:bg-gray-100 dark:hover:bg-gray-700"
        },
        {
            title: "Edit",
            icon: "M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z",
            onlyCurrentUser: true,
            className: "hover:bg-gray-100 dark:hover:bg-gray-700"
        },
        {
            title: "Delete",
            icon: "M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16",
            onlyCurrentUser: true,
            className: "hover:bg-red-100 dark:hover:bg-red-900 text-red-600"
        }
    ];

    actions.forEach(action => {
        if (!action.onlyCurrentUser || (action.onlyCurrentUser && isCurrentUser)) {
            const button = document.createElement("button");
            button.className = `action-btn p-1.5 rounded transition-colors duration-150 ${action.className}`;
            button.title = action.title;
            button.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="${action.icon}" />
        </svg>
      `;
            actionsMenu.appendChild(button);
        }
    });

    return actionsMenu;
};

const styles = `
  
  .highlight-animation {
    animation: highlightFade 2s ease-out;
  }
  
  @keyframes highlightFade {
    0% {
      background-color: rgba(79, 70, 229, 0.2);
    }
    100% {
      background-color: transparent;
    }
  }
  
  /* Update hover background for better contrast with highlight */
  .message:hover {
    background-color: rgba(0, 0, 0, 0.03);
  }
  
  .dark .message:hover {
    background-color: rgba(255, 255, 255, 0.03);
  }
  `;

document.head.insertAdjacentHTML('beforeend', `<style>${styles}</style>`);

function addEventListeners(element, messageId, messageText) {
    const actionButtons = element.querySelectorAll('.action-btn');

    actionButtons.forEach(button => {
        const buttonTitle = button.getAttribute('title');

        switch (buttonTitle) {
            case 'Edit':
                button.addEventListener('click', () => editMessage(messageId));
                break;

            case 'Delete':
                button.addEventListener('click', () => deleteMessage(messageId));
                break;

            case 'Reply':
                button.addEventListener('click', () => startReply(messageId, messageText));
                break;

            case 'React':
                button.addEventListener('click', () => createReactionPicker(messageId));
                break;
        }
    });
}

const addMessageToDOM = (element) => {
    let messageContainer = messages.querySelector('.flex.flex-col');
    if (!messageContainer) {
        messageContainer = document.createElement('div');
        messageContainer.className = 'flex flex-col space-y-4 p-4';
        messages.appendChild(messageContainer);
    }

    // Always append new messages to the end of the container
    messageContainer.appendChild(element);

    messages.scrollTop = messages.scrollHeight;

    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const messageId = entry.target.getAttribute('data-message-id');
                if (unreadMessages.has(messageId)) {
                    markMessagesAsRead();
                }
                observer.unobserve(entry.target);
            }
        });
    }, {
        threshold: 1.0
    });

    observer.observe(element);
};

const scrollToMessage = async (messageId) => {
    const targetMessage = document.querySelector(`[data-message-id="${messageId}"]`);

    if (targetMessage) {
        highlightAndScrollTo(targetMessage);
    } else {
        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'fixed top-4 right-4 bg-blue-500 text-white px-4 py-2 rounded shadow-lg';
        loadingDiv.textContent = 'Loading message...';
        document.body.appendChild(loadingDiv);

        // Request message and wait for response
        return new Promise((resolve) => {
            socketio.emit('find_message', {
                message_id: messageId
            });

            socketio.once('message_found', (data) => {
                loadingDiv.remove();

                if (!data.found) {
                    const errorDiv = document.createElement('div');
                    errorDiv.className = 'fixed top-4 right-4 bg-red-500 text-white px-4 py-2 rounded shadow-lg';
                    errorDiv.textContent = 'Original message not found';
                    document.body.appendChild(errorDiv);
                    setTimeout(() => errorDiv.remove(), 3000);
                    resolve(false);
                    return;
                }

                // Update messages container
                const messageContainer = messages.querySelector('.flex.flex-col') || document.createElement('div');
                messageContainer.className = 'flex flex-col space-y-4 p-4';
                messageContainer.innerHTML = '';
                messages.innerHTML = '';
                messages.appendChild(messageContainer);

                // Add received messages
                data.messages.forEach(message => {
                    const messageElement = createMessageElement(
                        message.name,
                        message.message,
                        message.image,
                        message.id,
                        message.reply_to,
                        message.edited || false,
                        message.reactions || {},
                        message.read_by || [],
                        message.type || 'normal',
                        message.video,
                        message.timestamp
                    );
                    messageContainer.appendChild(messageElement);
                });

                hasMoreMessages = data.has_more;
                updateLoadMoreButton();

                // Find and scroll after messages are loaded
                setTimeout(() => {
                    const targetMessage = document.querySelector(`[data-message-id="${messageId}"]`);
                    if (targetMessage) {
                        highlightAndScrollTo(targetMessage);
                    }
                    resolve(true);
                }, 100);
            });
        });
    }
};

const highlightAndScrollTo = (element) => {
    element.scrollIntoView({
        behavior: 'smooth',
        block: 'center'
    });

    // Add noticeable highlight styles
    element.style.transition = 'background-color 0.3s, border 0.3s';
    element.style.backgroundColor = 'rgba(255, 230, 100, 0.6)'; // Bright yellowish color
    element.style.border = '2px solid rgba(255, 193, 7, 0.9)'; // Strong border

    // Remove the highlight after 2 seconds
    setTimeout(() => {
        element.style.backgroundColor = '';
        element.style.border = '';
    }, 5000);
};


const stylessss = document.createElement('style');
stylessss.textContent = `
  .reply-indicator {
  position: fixed;
  bottom: 80px;  /* Adjust based on your input area height */
  left: 0;
  right: 0;
  padding: 8px 16px;
  background: rgba(59, 130, 246, 0.1);
  backdrop-filter: blur(8px);
  border-top: 1px solid rgba(59, 130, 246, 0.2);
  transform: translateY(100%);
  transition: transform 0.2s ease-out;
  z-index: 50;
  display: none;
  }
  
  .reply-indicator.active {
  transform: translateY(0);
  display: flex;
  }
  
  .reply-content {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-right: 8px;
  }
  
  .replying {
  border-left: 4px solid rgb(59, 130, 246) !important;
  padding-left: 12px !important;
  }
  
  .cancel-reply {
  padding: 4px 8px;
  border-radius: 4px;
  background: rgba(239, 68, 68, 0.1);
  color: rgb(239, 68, 68);
  transition: all 0.2s;
  }
  
  .cancel-reply:hover {
  background: rgba(239, 68, 68, 0.2);
  }
  `;
document.head.appendChild(stylessss);

// Create reply indicator element
const replyIndicator = document.createElement('div');
replyIndicator.className = 'reply-indicator';
replyIndicator.innerHTML = `
  <div class="reply-content">
    <div class="text-sm font-medium text-blue-500">Replying to:</div>
    <div class="text-sm message-preview"></div>
  </div>
  <button class="cancel-reply text-sm font-medium">
    Cancel
  </button>
  `;
document.body.appendChild(replyIndicator);

const startReply = (messageId, message) => {
    // Store reply information
    replyingTo = {
        id: messageId,
        message: message
    };

    // Update input placeholder and styling
    messageInput.placeholder = "Type your reply...";
    messageInput.classList.add('replying');
    messageInput.focus();

    // Show reply indicator
    const messagePreview = replyIndicator.querySelector('.message-preview');
    messagePreview.textContent = message;
    replyIndicator.classList.add('active');

    // Scroll the message into view with highlighting
    const targetMessage = document.querySelector(`[data-message-id="${messageId}"]`);
    if (targetMessage) {
        targetMessage.scrollIntoView({
            behavior: 'smooth',
            block: 'center'
        });
        targetMessage.style.transition = 'background-color 0.3s ease';
        targetMessage.style.backgroundColor = 'rgba(59, 130, 246, 0.1)';
        setTimeout(() => {
            targetMessage.style.backgroundColor = '';
        }, 1500);
    }
};

const cancelReply = () => {
    replyingTo = null;
    messageInput.placeholder = "Type a message...";
    messageInput.classList.remove('replying');
    replyIndicator.classList.remove('active');
};

// Add event listener for cancel button
replyIndicator.querySelector('.cancel-reply').addEventListener('click', cancelReply);

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && replyingTo) {
        cancelReply();
    }
});


socketio.on('update_reactions', ({
    messageId,
    reactions
}) => {
    const message = document.querySelector(`[data-message-id="${messageId}"]`);
    if (!message) return;

    const container = message.querySelector('.reactions-container');
    if (!container) return;

    // Handle the update with animations
    updateReactionsDisplay(container, reactions, messageId);
});

const editMessage = (messageId) => {
    const messageElement = document.querySelector(`[data-message-id="${messageId}"]`);
    if (!messageElement) return;

    const messageContent = messageElement.querySelector('.message-content');
    if (!messageContent) return;

    const currentText = messageContent.textContent;
    const isCurrentUser = messageElement.classList.contains('bg-indigo-700');

    // Create input element
    const input = document.createElement('input');
    input.type = 'text';
    input.value = currentText;
    input.className = `rounded-md p-1 w-full ${
      isCurrentUser 
          ? 'bg-indigo-700 text-white placeholder-indigo-300 border border-indigo-400' 
          : 'bg-white text-gray-900 border border-gray-300'
  }`;

    messageContent.replaceWith(input);
    input.focus();

    const handleEdit = (event) => {
        if (event.key === 'Enter' || event.type === 'blur') {
            const newText = input.value.trim();
            if (newText !== '' && newText !== currentText) {
                socketio.emit('edit_message', {
                    messageId,
                    newText
                });
            }
            finishEdit(newText || currentText, isCurrentUser);
        } else if (event.key === 'Escape') {
            finishEdit(currentText, isCurrentUser);
        }
    };

    const finishEdit = (text, isCurrentUser) => {
        input.removeEventListener('keyup', handleEdit);
        input.removeEventListener('blur', handleEdit);

        const newMessageContent = document.createElement('div');
        newMessageContent.className = `message-content ${isCurrentUser ? 'text-white' : 'text-gray-900'}`;
        newMessageContent.textContent = text;

        input.replaceWith(newMessageContent);
    };

    input.addEventListener('keyup', handleEdit);
    input.addEventListener('blur', handleEdit);
};

const styleSheet = document.createElement("style");
styleSheet.textContent = `
  @keyframes messageBurst {
  0% {
    clip-path: polygon(0 0, 100% 0, 100% 100%, 0 100%);
    opacity: 1;
    transform: scale(1);
  }
  
  15% {
    clip-path: polygon(
      10% 10%, 30% 0, 50% 10%, 70% 0, 90% 10%,
      100% 30%, 90% 50%, 100% 70%, 90% 90%,
      70% 100%, 50% 90%, 30% 100%, 10% 90%,
      0 70%, 10% 50%, 0 30%
    );
    transform: scale(1.1);
  }
  
  30% {
    clip-path: polygon(
      5% 5%, 25% 5%, 50% 20%, 75% 5%, 95% 5%,
      95% 25%, 80% 50%, 95% 75%, 95% 95%,
      75% 95%, 50% 80%, 25% 95%, 5% 95%,
      5% 75%, 20% 50%, 5% 25%
    );
    opacity: 0.8;
    transform: scale(1.2);
  }
  
  50% {
    clip-path: polygon(
      0 0, 20% 10%, 40% 0, 60% 10%, 80% 0,
      100% 20%, 90% 40%, 100% 60%, 90% 80%,
      80% 100%, 60% 90%, 40% 100%, 20% 90%,
      0 80%, 10% 60%, 0 40%
    );
    opacity: 0.6;
    transform: scale(1.4) translateY(-5px);
  }
  
  75% {
    clip-path: polygon(
      10% 0, 30% 0, 50% 20%, 70% 0, 90% 0,
      100% 30%, 80% 50%, 100% 70%, 90% 100%,
      70% 100%, 50% 80%, 30% 100%, 10% 100%,
      0 70%, 20% 50%, 0 30%
    );
    opacity: 0.3;
    transform: scale(1.6) translateY(-10px);
  }
  
  100% {
    clip-path: polygon(
      15% 5%, 25% 0, 40% 10%, 60% 0, 85% 5%,
      100% 15%, 95% 35%, 100% 60%, 85% 85%,
      60% 100%, 40% 95%, 25% 100%, 10% 85%,
      0 60%, 5% 35%, 0 15%
    );
    opacity: 0;
    transform: scale(1.8) translateY(-15px);
  }
  }
  
  .message-deleting .relative > div:last-child {
  animation: messageBurst 1s ease-out forwards;
  }
  
  /* Crack line overlays */
  .message-deleting .relative > div:last-child::before,
  .message-deleting .relative > div:last-child::after {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.2s ease-in-out;
  }
  
  .message-deleting .relative > div:last-child::before {
  background: linear-gradient(45deg, 
    transparent 45%, 
    rgba(255, 255, 255, 0.2) 45%, 
    rgba(255, 255, 255, 0.2) 55%, 
    transparent 55%
  );
  animation: burstLines 0.5s ease-in forwards;
  }
  
  .message-deleting .relative > div:last-child::after {
  background: linear-gradient(-45deg, 
    transparent 45%, 
    rgba(0, 0, 0, 0.1) 45%, 
    rgba(0, 0, 0, 0.1) 55%, 
    transparent 55%
  );
  animation: burstLines 0.5s ease-in 0.1s forwards;
  }
  
  @keyframes burstLines {
  0% {
    opacity: 0;
  }
  50% {
    opacity: 1;
  }
  100% {
    opacity: 0;
  }
  }
  
  /* Dark mode adjustments */
  @media (prefers-color-scheme: dark) {
  .message-deleting .relative > div:last-child::before {
    background: linear-gradient(45deg, 
      transparent 45%, 
      rgba(255, 255, 255, 0.1) 45%, 
      rgba(255, 255, 255, 0.1) 55%, 
      transparent 55%
    );
  }
  
  .message-deleting .relative > div:last-child::after {
    background: linear-gradient(-45deg, 
      transparent 45%, 
      rgba(255, 255, 255, 0.05) 45%, 
      rgba(255, 255, 255, 0.05) 55%, 
      transparent 55%
    );
  }
  }
  `;

document.head.appendChild(styleSheet);

// Updated delete message function
const deleteMessage = async (messageId) => {
    if (!confirm('Are you sure you want to delete this message?')) return;

    const messageElement = document.querySelector(`[data-message-id="${messageId}"]`);
    if (!messageElement) return;

    try {
        messageElement.classList.add('message-deleting');

        // Check if message contains media (image or video)
        const mediaElement = messageElement.querySelector('img:not(.profile-photo), video');
        if (mediaElement) {
            try {
                const mediaUrl = mediaElement.src;
                const mediaPath = decodeURIComponent(mediaUrl.split('/o/')[1].split('?')[0]);
                const mediaRef = ref(storage, mediaPath);

                await deleteObject(mediaRef);
                console.log('Media deleted successfully from storage');
            } catch (error) {
                console.error('Error deleting media from storage:', error);
            }
        }

        socketio.emit('delete_message', {
            messageId
        });

        messageElement.addEventListener('animationend', () => {
            messageElement.remove();
        }, {
            once: true
        });

    } catch (error) {
        console.error('Error during message deletion:', error);
        messageElement.classList.remove('message-deleting');
        alert('Failed to delete message. Please try again.');
    }
};

fileUpload.setAttribute('accept', 'image/*,video/*');

fileUpload.addEventListener('change', async (event) => {
    const file = event.target.files[0];
    if (!file) return;

    try {
        const isVideo = file.type.startsWith('video/');
        const maxSize = isVideo ? 50 * 1024 * 1024 : 5 * 1024 * 1024; // 50MB for videos, 5MB for images

        if (file.size > maxSize) {
            alert(`File size exceeds maximum limit (${isVideo ? '50MB' : '5MB'})`);
            fileUpload.value = '';
            return;
        }

        // Create storage reference
        const storageRef = ref(storage, `${isVideo ? 'chat-videos' : 'chat-images'}/${Date.now()}_${file.name}`);

        // Show upload progress
        const progressIndicator = document.createElement('div');
        progressIndicator.className = 'upload-progress bg-gray-200 rounded-full h-2 mx-4 mb-4';
        progressIndicator.innerHTML = '<div class="bg-blue-600 h-2 rounded-full" style="width: 0%"></div>';
        messages.insertAdjacentElement('beforeend', progressIndicator);

        // Upload file
        const uploadTask = uploadBytesResumable(storageRef, file);

        uploadTask.on('state_changed',
            (snapshot) => {
                const progress = (snapshot.bytesTransferred / snapshot.totalBytes) * 100;
                progressIndicator.querySelector('div').style.width = progress + '%';
            },
            (error) => {
                console.error("Upload failed:", error);
                progressIndicator.remove();
                alert('Failed to upload file. Please try again.');
            },
            async () => {
                try {
                    const downloadURL = await getDownloadURL(uploadTask.snapshot.ref);
                    progressIndicator.remove();

                    // Emit socket event with the media URL
                    socketio.emit("message", {
                        data: isVideo ? "Sent a video" : "Sent an image",
                        [isVideo ? 'video' : 'image']: downloadURL,
                        replyTo: replyingTo ? replyingTo.id : null
                    });

                    fileUpload.value = '';

                } catch (error) {
                    console.error("Error getting download URL:", error);
                    alert('Failed to get media URL. Please try again.');
                }
            }
        );
    } catch (error) {
        console.error("Error handling file upload:", error);
        alert('Failed to process file. Please try again.');
    }
});

const markMessagesAsRead = () => {
    if (isTabActive && unreadMessages.size > 0) {
        const messageIds = Array.from(unreadMessages);
        socketio.emit("mark_messages_read", {
            message_ids: messageIds
        });
        unreadMessages.clear();
        unreadCount = 0;
        updatePageTitle();
        lastReadMessageId = messageIds[messageIds.length - 1];
        updateLocalStorage(LS_KEYS.LAST_READ_MESSAGE_ID, lastReadMessageId);
    }
};

const findMessageById = (messageId) => {
    const messageElement = document.querySelector(`[data-message-id="${messageId}"]`);
    if (messageElement) {
        const messageContent = messageElement.querySelector('.message-content');
        return messageContent ? messageContent.textContent : null;
    }
    return null;
};

socketio.on("message", (data) => {
    // Handle system messages
    if (data.type === 'system') {
        const messageElement = createMessageElement(
            data.name,
            data.message,
            null,
            data.id,
            null,
            false, {},
            data.read_by || [],
            'system',
            null,
            data.timestamp
        );
        addMessageToDOM(messageElement);
        return;
    }

    // Handle regular messages
    let replyToData = null;
    if (data.reply_to) {
        replyToData = {
            id: data.reply_to.id || data.reply_to,
            message: data.reply_to.message || findMessageById(data.reply_to)
        };
    }

    const messageElement = createMessageElement(
        data.name,
        data.message,
        data.image,
        data.id,
        replyToData,
        data.edited || false,
        data.reactions || {},
        data.read_by || [],
        'normal',
        data.video,
        data.timestamp,
        data.gif  // Add GIF parameter
    );
    addMessageToDOM(messageElement);

    // Handle unread messages
    if (data.name !== currentUser) {
        unreadMessages.add(data.id);

        if (isTabActive) {
            markMessagesAsRead();
        } else {
            unreadCount++;
            updatePageTitle();
        }
    }

    // Add action buttons
    const actionButtons = messageElement.querySelectorAll('.action-btn');
    actionButtons.forEach(button => {
        const buttonTitle = button.getAttribute('title');

        switch (buttonTitle) {
            case 'Edit':
                if (data.name === currentUser) {
                    button.addEventListener('click', () => editMessage(data.id));
                }
                break;

            case 'Delete':
                if (data.name === currentUser) {
                    button.addEventListener('click', () => deleteMessage(data.id));
                }
                break;

            case 'Reply':
                button.addEventListener('click', () => startReply(data.id, data.message || 'Sent a GIF'));
                break;

            case 'React':
                button.addEventListener('click', () => createReactionPicker(data.id));
                break;
        }
    });
});

socketio.on("chat_history", (data) => {
    const messageContainer = document.createElement('div');
    messageContainer.className = 'flex flex-col space-y-4 p-4';

    data.messages.forEach((message) => {
        // Handle system messages
        if (message.type === 'system') {
            const messageElement = createMessageElement(
                message.name,
                message.message,
                null,
                message.id,
                null,
                false,
                {},
                [],
                'system',
                null,
                message.timestamp
            );
            messageContainer.appendChild(messageElement);
            return;
        }

        // Handle regular messages including GIFs
        const validReaders = (message.read_by || []).filter(reader =>
            reader !== null &&
            reader !== message.name
        );

        const replyToData = message.reply_to ? {
            id: message.reply_to.id || message.reply_to,
            message: message.reply_to.message || findMessageById(message.reply_to)
        } : null;

        const messageElement = createMessageElement(
            message.name,
            message.message,
            message.image,
            message.id,
            replyToData,
            message.edited || false,
            message.reactions || {},
            validReaders,
            'normal',
            message.video,
            message.timestamp,
            message.gif  // Pass the gif data
        );
        messageContainer.appendChild(messageElement);

        if (message.name !== currentUser && !validReaders.includes(currentUser)) {
            unreadMessages.add(message.id);
        }
    });

    messages.innerHTML = '';
    messages.appendChild(messageContainer);

    if (data.messages.length > 0) {
        oldestMessageId = data.messages[0].id;
    }

    hasMoreMessages = data.has_more;
    updateLoadMoreButton();

    markMessagesAsRead();

    messages.scrollTop = messages.scrollHeight;
});

socketio.on('message_found', (data) => {
    const loadingDiv = document.querySelector('.fixed.top-4.right-4.bg-blue-500');
    if (loadingDiv) loadingDiv.remove();

    if (!data.found) {
        const errorDiv = document.createElement('div');
        errorDiv.className = 'fixed top-4 right-4 bg-red-500 text-white px-4 py-2 rounded shadow-lg';
        errorDiv.textContent = 'Original message not found';
        document.body.appendChild(errorDiv);
        setTimeout(() => errorDiv.remove(), 3000);
        return;
    }

    // Clear existing messages
    const messageContainer = messages.querySelector('.flex.flex-col') || document.createElement('div');
    messageContainer.className = 'flex flex-col space-y-4 p-4';
    messageContainer.innerHTML = '';
    messages.innerHTML = '';
    messages.appendChild(messageContainer);

    // Add messages
    data.messages.forEach(message => {
        const messageElement = createMessageElement(
            message.name,
            message.message,
            message.image,
            message.id,
            message.reply_to,
            message.edited || false,
            message.reactions || {},
            message.read_by || [],
            message.type || 'normal',
            message.video,
            message.gif,
            message.timestamp
        );
        messageContainer.appendChild(messageElement);
    });

    // Update pagination state
    hasMoreMessages = data.has_more;
    updateLoadMoreButton();

    // Find and highlight target message
    const targetMessage = document.querySelector(`[data-message-id="${message.id}"]`);
    if (targetMessage) {
        highlightAndScrollTo(targetMessage);
    }
});

socketio.on("more_messages", (data) => {
    const oldScrollHeight = messages.scrollHeight;
    let messageContainer = messages.querySelector('.flex.flex-col');

    if (!messageContainer) {
        messageContainer = document.createElement('div');
        messageContainer.className = 'flex flex-col space-y-4 p-4';
        messages.appendChild(messageContainer);
    }

    const loadMoreBtn = document.getElementById('load-more-btn');
    const fragment = document.createDocumentFragment();

    data.messages.forEach((message) => {
        // Handle system messages
        if (message.type === 'system') {
            const messageElement = createMessageElement(
                message.name,
                message.message,
                null, // system messages don't have images
                message.id,
                null, // system messages don't have replies
                false, // system messages can't be edited
                {}, // system messages don't have reactions
                [], // system messages don't track read status
                'system',
                null, // No video for system messages
                message.timestamp // Include timestamp
            );
            fragment.appendChild(messageElement);
            return;
        }

        // Handle regular messages including GIFs
        const validReaders = (message.read_by || []).filter(reader =>
            reader !== null &&
            reader !== message.name
        );

        const replyToData = message.reply_to ? {
            id: message.reply_to.id || message.reply_to,
            message: message.reply_to.message || findMessageById(message.reply_to)
        } : null;

        const messageElement = createMessageElement(
            message.name,
            message.message,
            message.image,
            message.id,
            replyToData,
            message.edited || false,
            message.reactions || {},
            validReaders,
            'normal',
            message.video,
            message.timestamp, // Include timestamp
            message.gif // Include GIF data
        );
        fragment.appendChild(messageElement);

        if (message.name !== currentUser && !validReaders.includes(currentUser)) {
            unreadMessages.add(message.id);
        }
    });

    if (loadMoreBtn) {
        loadMoreBtn.after(fragment);
    } else {
        messageContainer.insertBefore(fragment, messageContainer.firstChild);
    }

    if (data.messages.length > 0) {
        oldestMessageId = data.messages[0].id;
    }

    hasMoreMessages = data.has_more;
    updateLoadMoreButton();

    isLoadingMessages = false;

    const newScrollHeight = messages.scrollHeight;
    messages.scrollTop = newScrollHeight - oldScrollHeight + messages.scrollTop;
});

socketio.on("messages_read", (data) => {
    const {
        reader,
        message_ids
    } = data;

    message_ids.forEach(id => {
        const messageContainer = document.querySelector(`[data-message-id="${id}"]`);
        if (messageContainer) {
            const messageSender = messageContainer.querySelector('.text-sm.font-medium')?.textContent;

            // Only update the visual status if the message was sent by the current user
            // and was read by someone else
            if ((!messageSender || messageSender === currentUser) && reader !== currentUser) {
                updateMessageReadStatus(messageContainer, true);
            }
        }
    });
});

function createLoadMoreButton() {
    const button = document.createElement('button');
    button.id = 'load-more-btn';
    button.className = 'bg-blue-500 hover:bg-blue-700 text-white font-bold py-2 px-4 rounded mt-4 mb-4 w-full';
    button.textContent = 'Load More Messages';
    button.addEventListener('click', loadMoreMessages);
    return button;
}

function updateLoadMoreButton() {
    let messageContainer = messages.querySelector('.flex.flex-col');
    if (!messageContainer) {
        messageContainer = document.createElement('div');
        messageContainer.className = 'flex flex-col space-y-4 p-4';
        messages.appendChild(messageContainer);
    }

    let existingButton = document.getElementById('load-more-btn');
    if (hasMoreMessages) {
        if (!existingButton) {
            existingButton = createLoadMoreButton();
            messageContainer.insertBefore(existingButton, messageContainer.firstChild);
        }
    } else {
        if (existingButton) {
            existingButton.remove();
        }
    }
}

function loadMoreMessages() {
    if (isLoadingMessages || !hasMoreMessages) return;

    isLoadingMessages = true;
    socketio.emit("load_more_messages", {
        last_message_id: oldestMessageId
    });
}

socketio.on("edit_message", (data) => {
    const messageElement = document.querySelector(`[data-message-id="${data.messageId}"]`);
    if (messageElement) {
        const messageContainer = messageElement.querySelector('.message-content').parentElement;
        const isCurrentUser = messageElement.closest('.message').classList.contains('justify-end');

        // Update message content
        const messageContent = messageContainer.querySelector('.message-content');
        messageContent.textContent = data.newText;

        // Add edit indicator if not already present
        if (!messageContainer.querySelector('.edited-indicator')) {
            const editedIndicator = document.createElement("span");
            editedIndicator.className = `edited-indicator text-xs ${isCurrentUser ? 'text-white/70' : 'text-gray-500 dark:text-gray-400'}`;
            editedIndicator.textContent = "(edited)";
            messageContainer.appendChild(editedIndicator);
        }
    }
});

socketio.on("delete_message", (data) => {
    const messageElement = document.querySelector(`[data-message-id="${data.messageId}"]`);
    if (messageElement) {
        messageElement.classList.add('message-deleting');
        createShards(messageElement);

        messageElement.addEventListener('animationend', (e) => {
            if (e.target === messageElement.firstElementChild) {
                messageElement.remove();
            }
        }, {
            once: true
        });
    }
});

socketio.on("connect", () => {
    console.log("Connected to server");
    currentUser = username;
});

socketio.on("disconnect", () => {
    console.log("Disconnected from server");
});

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById("sendButton").addEventListener("click", sendMessage);
});

// Call saveToLocalStorage before the page unloads
window.addEventListener('beforeunload', saveToLocalStorage);