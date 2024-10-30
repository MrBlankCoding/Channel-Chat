// Import the Firebase SDK
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
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
  appId: "1:822894243205:web:e129bcac94601e183e68ec",
  measurementId: "G-PL15EEFQDE"
};

// Initialize Firebase
const app = initializeApp(firebaseConfig);

// Get a reference to the storage service
const storage = getStorage(app);

// Constants and DOM elements
const TYPING_TIMEOUT = 1000;
const messages = document.getElementById("messages");
const messageInput = document.getElementById("message");
const imageUpload = document.getElementById('image-upload');
const username = document.getElementById("username").value;
const unreadMessages = new Set();
const originalTitle = document.title;

// State variables
let replyingTo = null;
let isUserListVisible = false;
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
  transports: ['websocket']  // Ensure only WebSocket is used
});

// Helper functions
const createTypingIndicator = () => {
  const typingIndicator = document.createElement("div");
  typingIndicator.className = "typing-indicator";
  typingIndicator.style.display = "none";
  messages.parentNode.insertBefore(typingIndicator, messages.nextSibling);
  return typingIndicator;
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

// Load data from Local Storage
const loadFromLocalStorage = () => {
  unreadCount = parseInt(localStorage.getItem(LS_KEYS.UNREAD_COUNT) || '0');
  lastReadMessageId = localStorage.getItem(LS_KEYS.LAST_READ_MESSAGE_ID);
  currentUser = localStorage.getItem(LS_KEYS.USERNAME) || username;

  updatePageTitle();
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

const typingIndicator = createTypingIndicator();

// Responsive message spacing variables
let MESSAGE = {
  padding: {
    vertical: 4,    // Controls space between messages
    horizontal: 16, // Side padding of message container
    bubble: 6       // Padding inside message bubble
  },
  text: {
    size: 14,       // Main message text size
    spacing: 2      // Space between elements (header to content)
  },
  maxWidth: {       // Maximum width based on screen size
    default: '85%', // Default maximum width for mobile screens
    tablet: '70%',  // Width for tablet-sized screens
    desktop: '60%', // Width for desktop screens
    largeDesktop: '50%' // Width for larger desktop screens
  },
  photo: {
    size: 24,       // Profile photo dimensions
    gap: 8          // Space between photo and message
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

const createMessageElement = (name, msg, image, messageId, replyTo, isEdited = false, reactions = {}, readBy = []) => {
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
  element.className = 'message group hover:bg-gray-50/5 transition-colors duration-200';

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
  messageContainer.style.maxWidth = getResponsiveMaxWidth(); // Set responsive max width
  messageContainer.className = "relative";

  // Message header
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

  // Message bubble
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

  // Reply section
  if (replyTo) {
    const replyInfo = document.createElement("div");
    replyInfo.style.marginBottom = `${MESSAGE.text.spacing}px`;
    replyInfo.className = `text-xs rounded px-2 py-0.5 border-l-2 ${
      isCurrentUser
        ? 'bg-blue-600/50 border-blue-300'
        : 'bg-gray-200 dark:bg-gray-700 border-gray-400 dark:border-gray-500'
    }`;
    replyInfo.dataset.replyTo = replyTo.id;
    replyInfo.innerHTML = `<span class="opacity-75">Replying to:</span> ${replyTo.message}`;
    messageBubble.appendChild(replyInfo);
  }

  // Message content
  const messageContent = document.createElement("div");
  messageContent.className = "message-content break-words";
  messageContent.textContent = msg || "Sent an image";

  if (isEdited) {
    const editedIndicator = document.createElement("span");
    editedIndicator.className = "ml-1 text-xs opacity-75";
    editedIndicator.textContent = "(edited)";
    messageContent.appendChild(editedIndicator);
  }

  messageBubble.appendChild(messageContent);

  // Image handling
  if (image) {
    const img = document.createElement("img");
    img.src = image;
    img.alt = "Uploaded image";
    img.style.marginTop = `${MESSAGE.text.spacing}px`;
    img.className = "max-w-[120px] rounded-lg shadow-sm hover:shadow-md transition-shadow duration-200 opacity-50";
    img.onload = () => img.classList.remove('opacity-50');
    img.onerror = () => {
      img.src = '/static/images/image-error.png';
      img.classList.remove('opacity-50');
    };
    messageBubble.appendChild(img);
  }

  // Reactions
  const reactionsContainer = document.createElement("div");
  reactionsContainer.style.marginTop = `${MESSAGE.text.spacing}px`;
  reactionsContainer.className = "flex flex-wrap gap-0.5";
  
  if (Object.keys(reactions).length > 0) {
    Object.entries(reactions).forEach(([emoji, reactionData]) => {
      if (reactionData.count > 0) {
        const reactionElement = createReactionElement(emoji, reactionData, messageId);
        reactionsContainer.appendChild(reactionElement);
      }
    });
  }
  
  messageBubble.appendChild(reactionsContainer);

  // Actions menu
  const actionsMenu = createActionsMenu(isCurrentUser);
  messageBubble.appendChild(actionsMenu);

  messageContainer.appendChild(messageBubble);
  element.appendChild(messageContainer);

  addEventListeners(messageBubble, messageId, msg);

  return element;
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

const createReactionElement = (emoji, reactionData, messageId) => {
  const element = document.createElement("button");
  element.className = `flex items-center space-x-1 text-xs rounded-full px-2 py-0.5 transition-colors ${
    reactionData.users.includes(currentUser)
      ? 'bg-gray-200 dark:bg-gray-700'
      : 'bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700'
  }`;
  
  element.onclick = () => socketio.emit('toggle_reaction', { messageId, emoji });
  
  const emojiSpan = document.createElement("span");
  emojiSpan.textContent = emoji;
  
  const countSpan = document.createElement("span");
  countSpan.textContent = reactionData.count;
  
  element.appendChild(emojiSpan);
  element.appendChild(countSpan);
  
  return element;
};

const createActionsMenu = (isCurrentUser) => {
  const actionsMenu = document.createElement("div");
  actionsMenu.className = `actions-menu opacity-0 group-hover:opacity-100 absolute -top-8 ${
    isCurrentUser ? 'right-0' : 'left-0'
  } flex items-center space-x-1 bg-white dark:bg-gray-800 shadow-lg rounded-lg p-1 transition-all duration-200 z-10`;

  const actions = [
    { 
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

const createReactionPicker = () => {
  const picker = document.createElement("div");
  picker.className = "reaction-picker absolute bottom-full left-0 mb-2 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-600 p-2 z-20";
  
  const commonEmojis = ['👍', '❤️', '😊', '🎉', '🤔', '👀', '🙌', '🔥'];
  
  const emojiContainer = document.createElement("div");
  emojiContainer.className = "flex space-x-1";
  
  commonEmojis.forEach(emoji => {
    const button = document.createElement("button");
    button.className = "hover:bg-gray-100 dark:hover:bg-gray-700 p-1.5 rounded-lg transition-colors duration-150";
    button.textContent = emoji;
    button.onclick = (e) => {
      e.stopPropagation();
      const messageId = picker.closest('[data-message-id]').dataset.messageId;
      socketio.emit('add_reaction', { messageId, emoji });
      picker.remove();
    };
    emojiContainer.appendChild(button);
  });
  
  picker.appendChild(emojiContainer);
  return picker;
};

const addEventListeners = (messageBubble, messageId, msg) => {
  const replyBtn = messageBubble.querySelector('button[title="Reply"]');
  const editBtn = messageBubble.querySelector('button[title="Edit"]');
  const deleteBtn = messageBubble.querySelector('button[title="Delete"]');
  const reactBtn = messageBubble.querySelector('button[title="React"]');

  if (replyBtn) {
    replyBtn.addEventListener('click', () => startReply(messageId, msg));
  }

  if (editBtn) {
    editBtn.addEventListener('click', () => editMessage(messageId));
  }

  if (deleteBtn) {
    deleteBtn.addEventListener('click', () => deleteMessage(messageId));
  }

  if (reactBtn) {
    reactBtn.addEventListener('click', (event) => {
      event.stopPropagation();
      // Remove any existing reaction pickers
      document.querySelectorAll('.reaction-picker').forEach(picker => picker.remove());
      const picker = createReactionPicker();
      messageBubble.appendChild(picker);
      
      // Close picker when clicking outside
      const closePickerOnClickOutside = (e) => {
        if (!picker.contains(e.target) && !reactBtn.contains(e.target)) {
          picker.remove();
          document.removeEventListener('click', closePickerOnClickOutside);
        }
      };
      setTimeout(() => document.addEventListener('click', closePickerOnClickOutside), 0);
    });
  }
};

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
  }, { threshold: 1.0 });

  observer.observe(element);
};

const scrollToMessage = (messageId) => {
  const targetMessage = document.querySelector(`[data-message-id="${messageId}"]`);
  if (targetMessage) {
    // Add a slight delay to ensure the DOM is ready
    setTimeout(() => {
      targetMessage.scrollIntoView({ behavior: 'smooth', block: 'center' });
      
      // Add highlight animation
      targetMessage.classList.add('highlight-message');
      
      // Remove highlight after animation
      setTimeout(() => {
        targetMessage.classList.remove('highlight-message');
      }, 2000);
    }, 100);
  }
};

const highlightMessage = (messageElement) => {
  // Add a temporary highlight class
  messageElement.classList.add('highlight-animation');
  
  // Remove the highlight after animation completes
  setTimeout(() => {
    messageElement.classList.remove('highlight-animation');
  }, 2000);
};

const startReply = (messageId, message) => {
  replyingTo = { id: messageId, message: message };
  messageInput.placeholder = `Replying to: ${message}`;
  messageInput.classList.add('replying');
  messageInput.focus();
};

const cancelReply = () => {
  replyingTo = null;
  messageInput.placeholder = "Type a message...";
  messageInput.classList.remove('replying');
};

const addReaction = (messageId, emoji) => {
  socketio.emit('add_reaction', { messageId, emoji });
};

socketio.on('update_reactions', (data) => {
  const messageElement = document.querySelector(`[data-message-id="${data.messageId}"]`);
  if (!messageElement) return;

  const reactionsContainer = messageElement.querySelector('.reactions-container');
  if (!reactionsContainer) return;

  // Clear existing reactions
  reactionsContainer.innerHTML = '';

  // Add updated reactions
  Object.entries(data.reactions).forEach(([emoji, reactionData]) => {
    if (reactionData.count > 0) {
      const reactionElement = createReactionElement(emoji, reactionData, data.messageId);
      reactionsContainer.appendChild(reactionElement);
    }
  });
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
              socketio.emit('edit_message', { messageId, newText });
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

const updateTypingIndicator = () => {
  const typingArray = Array.from(typingUsers);
  let typingText = '';

  if (typingArray.length === 1) {
    typingText = `${typingArray[0]} is typing...`;
  } else if (typingArray.length === 2) {
    typingText = `${typingArray[0]} and ${typingArray[1]} are typing...`;
  } else if (typingArray.length > 2) {
    typingText = `${typingArray[0]}, ${typingArray[1]}, and ${typingArray.length - 2} more are typing...`;
  }

  typingIndicator.textContent = typingText;
  typingIndicator.style.display = typingArray.length > 0 ? "block" : "none";
};

const deleteMessage = async (messageId) => {
  if (confirm('Are you sure you want to delete this message?')) {
    const messageElement = document.querySelector(`[data-message-id="${messageId}"]`);
    if (!messageElement) return;

    // Check if message contains an image
    const imageElement = messageElement.querySelector('img:not(.profile-photo)');
    if (imageElement) {
      try {
        // Extract the image path from the URL
        const imageUrl = imageElement.src;
        // Convert the full URL to a storage reference path
        const imagePath = decodeURIComponent(imageUrl.split('/o/')[1].split('?')[0]);
        const imageRef = ref(storage, imagePath);
        
        // Delete the image from Firebase Storage
        await deleteObject(imageRef);
        console.log('Image deleted successfully from storage');
      } catch (error) {
        console.error('Error deleting image from storage:', error);
        // Continue with message deletion even if image deletion fails
      }
    }

    // Emit socket event to delete message from database
    socketio.emit('delete_message', { messageId });
  }
};
const sendMessage = () => {
  const message = messageInput.value.trim();
  
  if (message || imageUpload.files.length > 0) {
    // If there's an image selected, handle it through the image upload event listener
    if (imageUpload.files.length > 0) {
      imageUpload.dispatchEvent(new Event('change'));
    } else {
      // Otherwise, just send the text message
      const messageData = {
        data: message,
        replyTo: replyingTo ? replyingTo.id : null
      };
      socketio.emit("message", messageData);
    }

    // Clear the input and reply state
    messageInput.value = "";
    cancelReply();

    // Clear the typing indicator
    clearTimeout(typingTimeout);
    socketio.emit("typing", { isTyping: false });
  }
};

// Event listeners
messageInput.addEventListener("keyup", (event) => {
  if (event.key === "Enter") {
    sendMessage();
  } else {
    socketio.emit("typing", { isTyping: true });
    clearTimeout(typingTimeout);
    typingTimeout = setTimeout(() => {
      socketio.emit("typing", { isTyping: false });
    }, TYPING_TIMEOUT);
  }
});

socketio.on('update_reactions', (data) => {
  const messageElement = document.querySelector(`[data-message-id="${data.messageId}"]`);
  if (!messageElement) return;

  const reactionsContainer = messageElement.querySelector('.reactions-container');
  if (!reactionsContainer) return;

  // Clear existing reactions
  reactionsContainer.innerHTML = '';

  // Add updated reactions
  Object.entries(data.reactions).forEach(([emoji, reactionData]) => {
    if (reactionData.count > 0) {
      const reactionElement = createReactionElement(emoji, reactionData, data.messageId);
      reactionsContainer.appendChild(reactionElement);
    }
  });
});

// Update reactions when receiving socket event
socketio.on('update_reactions', (data) => {
  const messageElement = document.querySelector(`[data-message-id="${data.messageId}"]`);
  if (!messageElement) return;

  const reactionsContainer = messageElement.querySelector('.reactions-container');
  reactionsContainer.innerHTML = '';

  Object.entries(data.reactions).forEach(([emoji, reactionData]) => {
    const isSelected = reactionData.users.includes(currentUser);
    const reactionElement = createReactionElement(emoji, reactionData.count, isSelected);
    reactionElement.onclick = () => {
      socketio.emit('add_reaction', { messageId: data.messageId, emoji });
    };
    reactionsContainer.appendChild(reactionElement);
  });
});

imageUpload.addEventListener('change', async (event) => {
  const file = event.target.files[0];
  if (file) {
    try {
      // Create a storage reference with a unique filename
      const storageRef = ref(storage, `chat-images/${Date.now()}_${file.name}`);
      
      // Show upload progress
      const progressIndicator = document.createElement('div');
      progressIndicator.className = 'upload-progress bg-gray-200 rounded-full h-2 mx-4 mb-4';
      progressIndicator.innerHTML = '<div class="bg-blue-600 h-2 rounded-full" style="width: 0%"></div>';
      messages.insertAdjacentElement('beforeend', progressIndicator);
      
      // Upload file with progress tracking
      const uploadTask = uploadBytesResumable(storageRef, file);
      
      // Monitor upload progress
      uploadTask.on('state_changed',
        (snapshot) => {
          const progress = (snapshot.bytesTransferred / snapshot.totalBytes) * 100;
          progressIndicator.querySelector('div').style.width = progress + '%';
        },
        (error) => {
          console.error("Upload failed:", error);
          progressIndicator.remove();
          alert('Failed to upload image. Please try again.');
        },
        async () => {
          try {
            // Get the download URL
            const downloadURL = await getDownloadURL(uploadTask.snapshot.ref);
            
            // Remove progress indicator
            progressIndicator.remove();
            
            // Emit socket event with the image URL and any reply reference
            socketio.emit("message", {
              data: "Sent an image",
              image: downloadURL,
              replyTo: replyingTo ? replyingTo.id : null
            });

            // Clear the file input value so the same file can be sent again if needed
            imageUpload.value = '';
            
          } catch (error) {
            console.error("Error getting download URL:", error);
            alert('Failed to get image URL. Please try again.');
          }
        }
      );
    } catch (error) {
      console.error("Error handling file upload:", error);
      alert('Failed to process image. Please try again.');
    }
  }
});

const handleReaction = (emoji, reactionsContainer) => {
  const existingReaction = reactionsContainer.querySelector(`span[data-emoji="${emoji}"]`);
  
  if (existingReaction) {
    let count = parseInt(existingReaction.getAttribute('data-count'));
    existingReaction.setAttribute('data-count', ++count);
    existingReaction.textContent = `${emoji} ${count}`;
  } else {
    const newReaction = document.createElement('span');
    newReaction.className = 'reaction';
    newReaction.setAttribute('data-emoji', emoji);
    newReaction.setAttribute('data-count', 1);
    newReaction.textContent = `${emoji} 1`;
    reactionsContainer.appendChild(newReaction);
  }
};


const markMessagesAsRead = () => {
  if (isTabActive && unreadMessages.size > 0) {
    const messageIds = Array.from(unreadMessages);
    socketio.emit("mark_messages_read", { message_ids: messageIds });
    unreadMessages.clear();
    unreadCount = 0;
    updatePageTitle();
    lastReadMessageId = messageIds[messageIds.length - 1];
    updateLocalStorage(LS_KEYS.LAST_READ_MESSAGE_ID, lastReadMessageId);
  }
};

socketio.on("message", (data) => {
  const messageElement = createMessageElement(
    data.name, 
    data.message, 
    data.image, 
    data.id, 
    data.reply_to,
    data.edited || false,
    data.reactions || {}
  );
  addMessageToDOM(messageElement);

  if (data.name !== currentUser) {
    unreadMessages.add(data.id);
    if (isTabActive) {
      markMessagesAsRead();
    } else {
      unreadCount++;
      updatePageTitle();
    }
  }

  const replyInfo = messageElement.querySelector('.reply-info');
  if (replyInfo) {
    replyInfo.addEventListener('click', () => scrollToMessage(replyInfo.getAttribute('data-reply-to')));
  }

  if (data.name === currentUser) {
    messageInput.value = "";
    cancelReply();

    const editBtn = messageElement.querySelector('.edit-btn');
    const deleteBtn = messageElement.querySelector('.delete-btn');
    editBtn.addEventListener('click', () => editMessage(data.id));
    deleteBtn.addEventListener('click', () => deleteMessage(data.id));
  } else {
    const replyBtn = messageElement.querySelector('.reply-btn');
    replyBtn.addEventListener('click', () => startReply(data.id, data.message));
  }
});

socketio.on("messages_read", (data) => {
  const { reader, message_ids } = data;
  
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

socketio.on("chat_history", (data) => {
  const messageContainer = document.createElement('div');
  messageContainer.className = 'flex flex-col space-y-4 p-4';
  
  data.messages.forEach((message) => {
    const validReaders = (message.read_by || []).filter(reader => 
      reader !== null && 
      reader !== message.name
    );
    
    const messageElement = createMessageElement(
      message.name, 
      message.message, 
      message.image, 
      message.id, 
      message.reply_to,
      message.edited || false,
      message.reactions || {},
      validReaders // Pass the valid readers to createMessageElement
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
    const validReaders = (message.read_by || []).filter(reader => 
      reader !== null && 
      reader !== message.name
    );
    
    const messageElement = createMessageElement(
      message.name, 
      message.message, 
      message.image, 
      message.id, 
      message.reply_to,
      message.edited || false,
      message.reactions || {},
      validReaders // Pass the valid readers to createMessageElement
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
  socketio.emit("load_more_messages", { last_message_id: oldestMessageId });
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
    // Check if message had an image and delete the image element reference
    const imageElement = messageElement.querySelector('img:not(.profile-photo)');
    if (imageElement) {
      imageElement.remove();
    }
    // Remove the entire message element
    messageElement.remove();
  }
});

socketio.on("typing", (data) => {
  if (data.isTyping) {
    typingUsers.add(data.name);
  } else {
    typingUsers.delete(data.name);
  }
  updateTypingIndicator();
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
  // Get message ID from URL parameters
  loadFromLocalStorage()
  const urlParams = new URLSearchParams(window.location.search);
  const highlightMessageId = urlParams.get('highlight');

  if (highlightMessageId) {
    // Remove the URL parameter without refreshing the page
    const newUrl = window.location.pathname + window.location.hash;
    window.history.replaceState({}, '', newUrl);

    // Function to attempt scrolling to the message
    const attemptScroll = () => {
      const targetMessage = document.querySelector(`[data-message-id="${highlightMessageId}"]`);
      if (targetMessage) {
        // Message found, scroll to it
        setTimeout(() => {
          targetMessage.scrollIntoView({ behavior: 'smooth', block: 'center' });
          highlightMessage(targetMessage);
        }, 100);
      } else {
        // Message not found yet, try again
        if (document.querySelector('.flex.flex-col.space-y-4')) {
          // Messages container exists but message not found
          // This might mean we're still loading messages
          setTimeout(attemptScroll, 100);
        }
      }
    };

    // Start attempting to scroll
    attemptScroll();
  }
});

// Call saveToLocalStorage before the page unloads
window.addEventListener('beforeunload', saveToLocalStorage);