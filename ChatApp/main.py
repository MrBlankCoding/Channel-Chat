# Standard library imports
import os
import random
import re
import base64
import io
from datetime import timedelta, datetime
from string import ascii_uppercase
import urllib.parse

# Third-party library imports
from flask import Flask, render_template, request, session, redirect, url_for, send_from_directory, flash, jsonify
from flask_socketio import join_room, leave_room, send, emit, SocketIO
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from firebase_admin import credentials, messaging, storage
import firebase_admin
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from PIL import Image
from pymongo import MongoClient
from fuzzywuzzy import fuzz
from bson import ObjectId
import imghdr

load_dotenv()

cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred, {
    'storageBucket': 'channelchat-7d679.appspot.com'
})

app = Flask(__name__)
scheduler = BackgroundScheduler()

@app.context_processor
def utility_processor():
    return dict(get_room_data=get_room_data)

app.secret_key = os.getenv("SECRET_KEY")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config['ALLOWED_IMAGE_TYPES'] = {'png', 'jpeg', 'jpg', 'gif'}
app.config['UPLOAD_FOLDER'] = 'uploads'

# Initialize MongoDB client using the URI from .env
client = MongoClient(os.getenv("MONGO_URI"))
db = client['chat_app_db']

# Collections
users_collection = db['users']
rooms_collection = db['rooms']
heartbeats_collection = db["heartbeats"]
users_collection.create_index([("username", 1)], unique=True)
users_collection.create_index([("friends", 1)])
users_collection.create_index([("current_room", 1)])
rooms_collection.create_index([("users", 1)])
rooms_collection.create_index([("messages.id", 1)])
users_collection.create_index([("fcm_token", 1)])

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# User class for Flask-Login
class User(UserMixin):
    def __init__(self, username):
        self.username = username
        self.id = username

    @staticmethod
    def get(username):
        user_data = users_collection.find_one({"username": username})
        if not user_data:
            return None
        return User(username)

@login_manager.user_loader
def load_user(username):
    return User.get(username)

# SOCKET initialization 
socketio = SocketIO(app, cors_allowed_origins='*')

def datetime_to_iso(dt):
    return dt.isoformat() if dt else None

class NotificationService:
    def __init__(self, users_collection):
        self.users_collection = users_collection
        
    def get_user_notification_settings(self, username):
        user = self.users_collection.find_one(
            {"username": username},
            {"notification_settings": 1}
        )
        return user.get("notification_settings", {
            "enabled": False,
            "direct_messages": True,
            "mentions": True,
            "group_messages": True,
            "sound_enabled": True,
            "vibration_enabled": True
        })

    def update_notification_settings(self, username, settings):
        return self.users_collection.update_one(
            {"username": username},
            {"$set": {"notification_settings": settings}}
        )

    def get_user_fcm_token(self, username):
        user = self.users_collection.find_one(
            {"username": username},
            {"fcm_token": 1}
        )
        return user.get("fcm_token") if user else None

    async def send_notification(self, recipient_username, notification_type, content):
        try:
            # Get recipient's notification settings and FCM token
            recipient = self.users_collection.find_one({
                "username": recipient_username
            })
            
            if not recipient:
                return False
                
            settings = recipient.get("notification_settings", {})
            fcm_token = recipient.get("fcm_token")
            
            # Check if notifications are enabled and the specific type is enabled
            if not settings.get("enabled") or not settings.get(notification_type, True):
                return False
                
            if not fcm_token:
                return False
                
            # Construct the notification based on type
            notification = self._build_notification(notification_type, content)
            
            # Create the Android notification channel settings
            android_config = messaging.AndroidConfig(
                notification=messaging.AndroidNotification(
                    icon='notification_icon',  # Reference to drawable resource
                    sound='notification_sound' if settings.get('sound_enabled') else None,
                    channel_id='default_channel'  # Must match the channel ID created in your Android app
                )
            )

            # Create the APNS (iOS) notification settings
            apns_config = messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound='notification_sound.wav' if settings.get('sound_enabled') else None,
                        badge=1
                    )
                )
            )
            
            # Send the notification through Firebase with platform-specific configs
            message = messaging.Message(
                notification=messaging.Notification(
                    title=notification["title"],
                    body=notification["body"]
                ),
                data=notification.get("data", {}),
                token=fcm_token,
                android=android_config,
                apns=apns_config
            )
            
            response = messaging.send(message)
            return bool(response)
            
        except Exception as e:
            print(f"Error sending notification: {str(e)}")
            return False
            
    def _build_notification(self, notification_type, content):
        notifications = {
            "direct_message": {
                "title": f"New message from {content['sender']}",
                "body": content['message'][:100] + ('...' if len(content['message']) > 100 else ''),
                "data": {
                    "type": "direct_message",
                    "sender": content['sender'],
                    "room_id": content.get('room_id', '')
                }
            },
            "mention": {
                "title": f"{content['sender']} mentioned you",
                "body": content['message'][:100] + ('...' if len(content['message']) > 100 else ''),
                "data": {
                    "type": "mention",
                    "sender": content['sender'],
                    "room_id": content.get('room_id', '')
                }
            },
            "group_message": {
                "title": f"New message in {content['room_name']}",
                "body": f"{content['sender']}: {content['message'][:100]}" + ('...' if len(content['message']) > 100 else ''),
                "data": {
                    "type": "group_message",
                    "sender": content['sender'],
                    "room_id": content['room_id'],
                    "room_name": content['room_name']
                }
            }
        }
        
        return notifications.get(notification_type, {
            "title": "New Notification",
            "body": "You have a new notification"
        })

# Initialize the notification service
notification_service = NotificationService(users_collection)

@app.route('/notification-settings', methods=['GET', 'POST'])
@login_required
def notification_settings():
    if request.method == 'GET':
        settings = notification_service.get_user_notification_settings(current_user.username)
        return jsonify(settings)
        
    elif request.method == 'POST':
        try:
            settings = request.json
            notification_service.update_notification_settings(current_user.username, settings)
            return jsonify({"message": "Settings updated successfully"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

@app.route('/register-fcm-token', methods=['POST'])
@login_required
def register_fcm_token():
    try:
        data = request.json
        fcm_token = data.get('token')
        clear_all = data.get('clearAll', False)
        
        if clear_all:
            # Clear the FCM token
            users_collection.update_one(
                {"username": current_user.username},
                {"$unset": {"fcm_token": "", "notification_settings.enabled": ""}}
            )
            return jsonify({"message": "Notification settings cleared"}), 200
            
        if not fcm_token:
            return jsonify({"error": "Token is required"}), 400
            
        # Update the user's FCM token and enable notifications
        users_collection.update_one(
            {"username": current_user.username},
            {
                "$set": {
                    "fcm_token": fcm_token,
                    "notification_settings.enabled": True
                }
            }
        )
        
        return jsonify({"message": "FCM token registered successfully"}), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 400
        
@app.route('/firebase-messaging-sw.js')
def serve_sw():
    root_dir = os.path.abspath(os.getcwd())  # Gets the current working directory (project root)
    return send_from_directory(root_dir, 'firebase-messaging-sw.js', mimetype='application/javascript')

def save_profile_photo(file, username, current_photo_url=None):
    """Helper function to save and process profile photos, including removing old ones without cache invalidation."""
    if not file:
        return None

    # Verify file type and size
    allowed_types = ['png', 'jpg', 'jpeg', 'gif']
    file_bytes = file.read()
    file_type = imghdr.what(None, h=file_bytes)
    
    if file_type not in allowed_types:
        flash("Invalid image type. Allowed types: PNG, JPG, GIF")
        return None

    # Check file size (optional, e.g., max 5MB)
    if len(file_bytes) > 5 * 1024 * 1024:
        flash("File too large. Maximum size is 5MB.")
        return None

    try:
        # Delete the old profile photo if it exists
        if current_photo_url:
            old_filename = current_photo_url.split('/')[-1]  # Extract the filename from the URL
            bucket = storage.bucket()
            old_blob = bucket.blob(old_filename)
            old_blob.delete()  # Remove the old profile photo from storage
        
        # Process image with PIL
        image = Image.open(io.BytesIO(file_bytes))
        
        # Resize image to a reasonable size (e.g., 200x200)
        image.thumbnail((200, 200))
        
        # Save image to a BytesIO object
        img_io = io.BytesIO()
        image.save(img_io, format=file_type.upper())
        img_io.seek(0)  # Seek to the beginning of the stream
        
        # Upload the new image to Firebase Cloud Storage
        bucket = storage.bucket()
        filename = f"profile_{username}.{file_type}"
        blob = bucket.blob(filename)
        blob.upload_from_file(img_io, content_type=f"image/{file_type}")
        
        # Make the image publicly accessible
        blob.make_public()
        
        # Return the public URL of the new image
        return blob.public_url
    except Exception as e:
        flash("Error processing profile photo: " + str(e))
        return None
    
# Set up the background scheduler
def check_inactive_users():
    threshold = datetime.utcnow() - timedelta(minutes=5)
    inactive_users = heartbeats_collection.find({"last_heartbeat": {"$lt": threshold}})
    
    for user in inactive_users:
        users_collection.update_one(
            {"username": user["username"]},
            {"$set": {"online": False}}
        )
        heartbeats_collection.delete_one({"_id": user["_id"]})

def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(func=check_inactive_users, trigger="interval", minutes=1)
        scheduler.start()

with app.app_context():
    start_scheduler()

@app.route("/heartbeat", methods=["POST"])
@login_required
def heartbeat():
    username = current_user.username
    heartbeats_collection.update_one(
        {"username": username},
        {"$set": {"last_heartbeat": datetime.utcnow()}},
        upsert=True
    )
    users_collection.update_one(
        {"username": username},
        {"$set": {"online": True}}
    )
    return "", 204

@app.route("/stop_heartbeat", methods=["POST"])
@login_required
def stop_heartbeat():
    username = current_user.username
    heartbeats_collection.delete_one({"username": username})
    users_collection.update_one(
        {"username": username},
        {"$set": {"online": False}}
    )
    return "", 204

@app.route('/profile_photos/<username>')
def profile_photo(username):
    """Serve the user's profile photo from Firebase Cloud Storage"""
    for ext in app.config['ALLOWED_IMAGE_TYPES']:
        filename = f"profile_{username}.{ext}"
        blob = storage.bucket().blob(filename)
        
        try:
            exists = blob.exists()
            if exists:
                return redirect(blob.public_url)
        except Exception as e:
            print(f"Error checking blob existence: {e}")

    # If no profile photo is found, return the default profile image
    return redirect(url_for('default_profile'))

@app.route('/default-profile')
def default_profile():
    # Serve the default profile image if no custom image exists
    return send_from_directory('static/images', 'default-profile.png')

def generate_unique_code(length):
    while True:
        code = ""
        for _ in range(length):
            code += random.choice(ascii_uppercase)
        
        if not rooms_collection.find_one({"_id": code}):
            break
    
    return code

def get_user_data(username):
    """Get user data from MongoDB"""
    user_data = users_collection.find_one({"username": username})
    if user_data:
        # Convert ObjectId to string for JSON serialization
        user_data['_id'] = str(user_data['_id'])
        # Ensure room_invites exists
        if "room_invites" not in user_data:
            user_data["room_invites"] = []
            users_collection.update_one(
                {"username": username},
                {"$set": {"room_invites": []}}
            )
    return user_data

# Helper function to update user data
def update_user_data(username, data):
    """Update user data in MongoDB"""
    if '_id' in data:
        del data['_id']  # Remove _id if present to avoid update errors
    users_collection.update_one(
        {"username": username},
        {"$set": data}
    )

def is_valid_username(username):
    return re.match("^[a-zA-Z0-9_.-]+$", username)

def is_strong_password(password):
    return len(password) >= 8 and any(c.isdigit() for c in password) and any(c.isalpha() for c in password)

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        # Input validation
        if not username or not password:
            flash("Username and password are required!")
            return redirect(url_for("register"))

        if not is_valid_username(username):
            flash("Username can only contain letters, numbers, dots, underscores, and hyphens.")
            return redirect(url_for("register"))

        if not is_strong_password(password):
            flash("Password must be at least 8 characters long and include letters and numbers.")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Passwords do not match!")
            return redirect(url_for("register"))

        if users_collection.find_one({"username": username}):
            flash("Username already exists!")
            return redirect(url_for("register"))

        # Store user in MongoDB
        user_data = {
            "username": username,
            "password": generate_password_hash(password),
            "friends": [],
            "friend_requests": [],
            "current_room": None,
            "online": False,
            "rooms": []
        }
        users_collection.insert_one(user_data)

        flash("Registration successful! Please login.")
        return redirect(url_for("login"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            flash("Username and password are required!")
            return redirect(url_for("login"))

        user_data = users_collection.find_one({"username": username})
        if not user_data:
            flash("Invalid username or password!")
            return redirect(url_for("login"))

        if not check_password_hash(user_data["password"], password):
            flash("Invalid username or password!")
            return redirect(url_for("login"))

        user = User(username)
        login_user(user, remember=True)
        
        # Initialize heartbeat for the user
        heartbeats_collection.update_one(
            {"username": username},
            {"$set": {"last_heartbeat": datetime.utcnow()}},
            upsert=True
        )

        return redirect(url_for("home"))

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    username = current_user.username
    
    # Remove heartbeat entry
    heartbeats_collection.delete_one({"username": username})
    
    # Update user's online status
    users_collection.update_one(
        {"username": username},
        {"$set": {"online": False}}
    )
    
    logout_user()
    flash("You have been logged out.")
    return redirect(url_for("login"))

@app.route("/delete_account", methods=["POST"])
@login_required
def delete_account():
    username = current_user.username

    # Remove user from all rooms they are part of
    rooms_collection.update_many(
        {"members": username},
        {"$pull": {"members": username}}
    )

    # Remove friend relationships
    users_collection.update_many(
        {"friends": username},
        {"$pull": {"friends": username}}
    )
    
    # Remove any pending friend requests sent by the user
    users_collection.update_many(
        {"friend_requests": username},
        {"$pull": {"friend_requests": username}}
    )

    # Remove any pending friend requests received by the user
    users_collection.update_one(
        {"username": username},
        {"$set": {"friend_requests": []}}
    )

    # Delete user's heartbeat entry if present
    heartbeats_collection.delete_one({"username": username})

    # Delete user from the users collection
    users_collection.delete_one({"username": username})

    # Log out the user after account deletion
    logout_user()

    flash("Your account has been deleted.")
    return redirect(url_for("register"))

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        current_password = request.form.get("current_password")
        new_password = request.form.get("new_password")
        confirm_new_password = request.form.get("confirm_new_password")
        profile_photo = request.files.get("profile_photo")
        
        username = current_user.username
        user_data = users_collection.find_one({"username": username})

        # Handle profile photo upload
        if profile_photo:
            public_url = save_profile_photo(profile_photo, username)
            if public_url:
                users_collection.update_one(
                    {"username": username},
                    {"$set": {"profile_photo": public_url}}
                )
                flash("Profile photo updated successfully!")
            else:
                flash("Failed to upload profile photo. Please try again.")
        
        # Validate current password
        if current_password and not check_password_hash(user_data["password"], current_password):
            flash("Current password is incorrect!")
            return redirect(url_for("settings"))
        
        # Update password
        if new_password:
            if not is_strong_password(new_password):
                flash("Password must be at least 8 characters long and include letters and numbers.")
                return redirect(url_for("settings"))
                
            if new_password != confirm_new_password:
                flash("New passwords do not match!")
                return redirect(url_for("settings"))
                
            users_collection.update_one(
                {"username": username},
                {"$set": {"password": generate_password_hash(new_password)}}
            )
            flash("Password updated successfully!")
        
        return redirect(url_for("settings"))
    
    user_data = users_collection.find_one({"username": current_user.username})
    return render_template("settings.html", user_data=user_data)

def handle_friend_request(username, friend_username):
    friend_data = users_collection.find_one({"username": friend_username})
    if not friend_data:
        flash("User not found!")
        return redirect(url_for("home"))
        
    if friend_username == username:
        flash("You cannot add yourself as a friend!")
        return redirect(url_for("home"))
        
    if username in friend_data.get("friends", []):
        flash("Already friends!")
        return redirect(url_for("home"))
        
    # Add friend request
    users_collection.update_one(
        {"username": friend_username},
        {"$addToSet": {"friend_requests": username}}
    )
    
    flash(f"Friend request sent to {friend_username}!")
    return redirect(url_for("home"))

@app.route("/add_friend", methods=["POST"])
@login_required
def add_friend():
    friend_username = request.form.get("friend_username")
    if not friend_username:
        flash("Please enter a username.")
        return redirect(url_for("home"))
    
    username = current_user.username
    if friend_username == username:
        flash("You cannot add yourself as a friend!")
        return redirect(url_for("home"))
    
    friend_data = users_collection.find_one({"username": friend_username})
    if not friend_data:
        flash("User not found!")
        return redirect(url_for("home"))
    
    user_data = users_collection.find_one({"username": username})
    
    # Check if they're already friends
    if friend_username in user_data.get("friends", []):
        flash("Already friends!")
        return redirect(url_for("home"))
    
    # Check if there's a pending request
    if friend_username in user_data.get("friend_requests", []):
        flash("This user has already sent you a friend request! Check your friend requests to accept it.")
        return redirect(url_for("home"))
    
    # Add friend request
    users_collection.update_one(
        {"username": friend_username},
        {"$addToSet": {"friend_requests": username}}
    )
    
    flash(f"Friend request sent to {friend_username}!")
    return redirect(url_for("home"))

@app.route("/accept_friend/<username>")
@login_required
def accept_friend(username):
    # Extract the username string from current_user
    current_username = current_user.username
    
    # Update both users' friend lists atomically
    result = users_collection.update_one(
        {
            "username": current_username,
            "friend_requests": username
        },
        {
            "$pull": {"friend_requests": username},
            "$addToSet": {"friends": username}
        }
    )
    
    if result.modified_count:
        users_collection.update_one(
            {"username": username},
            {"$addToSet": {"friends": current_username}}
        )
        flash(f"You are now friends with {username}!")
    else:
        flash("No friend request found!")
        
    return redirect(url_for("home"))

@app.route("/decline_friend/<username>")
@login_required
def decline_friend(username):
    # Assuming current_user has a 'username' attribute
    current_username = current_user.username

    result = users_collection.update_one(
        {"username": current_username},  # Use the actual username, not the LocalProxy object
        {"$pull": {"friend_requests": username}}
    )
    
    if result.modified_count:
        flash(f"Friend request from {username} declined.")
    else:
        flash("No friend request found!")
        
    return redirect(url_for("home"))

@app.route("/remove_friend/<username>", methods=["POST"])
@login_required
def remove_friend(username):
    current_username = current_user.username  # Access the username of the current_user object
    
    # Remove from both users' friend lists atomically
    result = users_collection.update_one(
        {
            "username": current_username,  # Use the extracted username
            "friends": username
        },
        {"$pull": {"friends": username}}
    )
    
    if result.modified_count:
        users_collection.update_one(
            {"username": username},
            {"$pull": {"friends": current_username}}  # Use the extracted username here as well
        )
        return jsonify({"success": True})
    
    return jsonify({"error": "Not friends"}), 400

@app.route("/", methods=["POST", "GET"])
@login_required
def home():
    username = current_user.username
    
    # Get or create user data
    user_data = users_collection.find_one({"username": username})
    if not user_data:
        # Initialize new user data if it doesn't exist
        user_data = {
            "username": username,
            "rooms": [],
            "friends": [],
            "friend_requests": [],
            "online": True,
            "current_room": None
        }
        users_collection.insert_one(user_data)
    
    if request.method == "POST":
        code = request.form.get("code")
        join = request.form.get("join", False)
        create = request.form.get("create", False)
        friend_username = request.form.get("friend_username")

        # Handle friend request
        if friend_username:
            return handle_friend_request(username, friend_username)

        # Handle room operations
        if join != False and not code:
            flash("Please enter a room code.")
            return redirect(url_for("home"))
        
        return handle_room_operation(username, code, create, join)

    # Get friends data with online status and current rooms
    friends_data = []
    for friend in user_data.get("friends", []):
        friend_data = users_collection.find_one({"username": friend})
        if friend_data:
            friends_data.append({
                "username": friend,
                "online": friend_data.get("online", False),
                "current_room": friend_data.get("current_room")
            })

    return render_template("homepage.html",
                         username=username,
                         user_data=user_data,
                         friends=friends_data,
                         friend_requests=user_data.get("friend_requests", []))
    
@app.route('/search_users', methods=["GET"])
@login_required
def search_users():
    query = request.args.get("q", "").lower().strip()
    if not query:
        return jsonify([])

    # Get current user's data for exclusion list
    current_user_data = users_collection.find_one({"username": current_user.username})
    friends_list = current_user_data.get("friends", [])

    # First, get all eligible users (excluding current user and friends)
    all_users = list(users_collection.find(
        {
            "username": {"$ne": current_user.username},
            "username": {"$nin": friends_list}
        },
        {"username": 1}  # We don't need to fetch profile_photo field since we're using the route
    ))

    # If it's just one character, only match first letter
    if len(query) == 1:
        matching_users = [
            user for user in all_users 
            if user["username"].lower().startswith(query)
        ]
    else:
        # For longer queries, use fuzzy matching
        username_matches = [
            (user, fuzz.ratio(query, user["username"].lower()))
            for user in all_users
        ]

        # Filter based on different criteria depending on query length
        if len(query) <= 3:
            matching_users = [
                user for user, score in username_matches
                if score > 50 or user["username"].lower().startswith(query)
            ]
        else:
            matching_users = [
                user for user, score in username_matches
                if score > 70
            ]

        # Sort by similarity score
        matching_users.sort(
            key=lambda x: fuzz.ratio(query, x["username"].lower()),
            reverse=True
        )

    # Limit results
    matching_users = matching_users[:5]

    # Format response using the profile_photo route
    suggestions = [{
        "username": user["username"],
        "profile_photo_url": url_for('profile_photo', username=user["username"], _external=True),
        "similarity": fuzz.ratio(query, user["username"].lower())
    } for user in matching_users]

    return jsonify(suggestions)
    
    
def delete_firebase_image(image_url):
    """Helper function to delete an image from Firebase Storage"""
    if not image_url:
        return
    
    try:
        # Extract the path from the Firebase Storage URL, similar to JS implementation
        # URL format: https://firebasestorage.googleapis.com/v0/b/[bucket]/o/[path]?token...
        path = image_url.split('/o/')[1].split('?')[0]
        # URL decode the path to handle special characters and spaces
        path = urllib.parse.unquote(path)
        
        # Get bucket and create blob reference
        bucket = storage.bucket()
        blob = bucket.blob(path)
        
        # Delete the blob
        blob.delete()
        print(f"Successfully deleted image: {path}")
    except Exception as e:
        print(f"Error deleting image from Firebase Storage: {e}")
        print(f"Attempted to delete path: {path}")

@app.route("/invite_to_room/<username>")
def invite_to_room(username):
    current_room = session.get("room")
    
    if not current_room:
        flash("You're not in a room.")
        return redirect(url_for("home"))
    
    # Get the friend's data
    friend_data = get_user_data(username)
    if not friend_data:
        flash("User not found.")
        return redirect(url_for("room"))
    
    # Get current user's data and ensure current_user is handled correctly
    current_username = current_user.username  # Extract username from LocalProxy
    user_data = get_user_data(current_username)
    
    if username not in user_data.get("friends", []):
        flash("You can only invite friends to rooms.")
        return redirect(url_for("room"))
    
    # Initialize room_invites if it doesn't exist
    if "room_invites" not in friend_data:
        friend_data["room_invites"] = []
    
    # Check if invite already exists
    existing_invite = next((inv for inv in friend_data["room_invites"] 
                          if inv.get("room") == current_room), None)
    
    if not existing_invite:
        # Create new invite with proper structure
        new_invite = {
            "room": current_room,
            "from": current_username,  # Use the actual username string here
        }
        friend_data["room_invites"].append(new_invite)
        
        # Save the updated friend data
        update_user_data(username, friend_data)
        flash(f"Room invitation sent to {username}!")
    else:
        flash(f"{username} already has a pending invite to this room.")
    
    return redirect(url_for("room"))

@app.route("/join_friend_room/<friend_username>")
@login_required
def join_friend_room(friend_username):
    username = current_user.username
    user_data = users_collection.find_one({"username": username})
    
    if friend_username not in user_data.get("friends", []):
        flash("User is not in your friends list.")
        return redirect(url_for("home"))
    
    friend_data = users_collection.find_one({"username": friend_username})
    friend_room = friend_data.get("current_room")
    
    if not friend_room:
        flash("Friend is not in any room.")
        return redirect(url_for("home"))
    
    room_exists = rooms_collection.find_one({"_id": friend_room})
    if not room_exists:
        flash("Friend's room no longer exists.")
        return redirect(url_for("home"))
    
    session["room"] = friend_room
    session["name"] = username
    
    # Update user's current room
    users_collection.update_one(
        {"username": username},
        {"$set": {"current_room": friend_room}}
    )
    
    return redirect(url_for("room"))

@app.route("/accept_room_invite/<room_code>")
@login_required
def accept_room_invite(room_code):
    username = current_user.username
    user_data = get_user_data(username)
    
    # Find and remove the invite
    invite_found = False
    room_invites = user_data.get("room_invites", [])
    
    # Filter out the accepted invite
    user_data["room_invites"] = [
        inv for inv in room_invites 
        if not (inv["room"] == room_code and not invite_found and (invite_found := True))
    ]
    
    if not invite_found:
        flash("Room invite not found or already accepted.")
        return redirect(url_for("home"))
    
    # Add room to user's rooms list
    if "rooms" not in user_data:
        user_data["rooms"] = []
    if room_code not in user_data["rooms"]:
        user_data["rooms"].append(room_code)
    
    # Save the updated user data
    update_user_data(username, user_data)
    flash("Room invite accepted!")
    return redirect(url_for("room", code=room_code))

@app.route("/decline_room_invite/<room_code>")
@login_required
def decline_room_invite(room_code):
    username = current_user.username
    user_data = get_user_data(username)
    
    # Remove the invite
    user_data["room_invites"] = [inv for inv in user_data.get("room_invites", []) 
                                if inv["room"] != room_code]
    
    update_user_data(username, user_data)
    flash("Room invite declined.")
    return redirect(url_for("home"))

@app.route("/delete_room/<room_code>")
@login_required
def delete_room(room_code):
    username = current_user.username
    room_data = rooms_collection.find_one({"_id": room_code})
    
    if not room_data:
        flash("Room does not exist.")
        return redirect(url_for("home"))
    
    if room_data["created_by"] != username:
        flash("You don't have permission to delete this room.")
        return redirect(url_for("home"))
    
    # Delete all images from Firebase Storage
    deleted_images = 0
    failed_deletions = 0
    
    if "messages" in room_data:
        for message in room_data["messages"]:
            if "image" in message and message["image"]:
                try:
                    delete_firebase_image(message["image"])
                    deleted_images += 1
                except Exception as e:
                    failed_deletions += 1
                    print(f"Failed to delete image for message: {message.get('id', 'unknown')}")
                    print(f"Error: {str(e)}")
    
    # Remove room from all users who are in it
    users_collection.update_many(
        {"rooms": room_code},
        {
            "$pull": {"rooms": room_code},
            "$set": {"current_room": None}
        }
    )
    
    # Delete the room
    rooms_collection.delete_one({"_id": room_code})
    
    # Provide feedback about the deletion process
    if deleted_images > 0 or failed_deletions > 0:
        flash(f"Room deleted. Successfully removed {deleted_images} images. {failed_deletions} images failed to delete.")
    else:
        flash("Room successfully deleted.")
        
    return redirect(url_for("home"))

def handle_room_operation(username, code, create, join):
    room = code
    if create:
        room = generate_unique_code(10)
        room_name = request.form.get('room_name', 'Unnamed Room')  # Get custom name from form
        rooms_collection.insert_one({
            "_id": room,
            "name": room_name,  # Add custom name
            "users": [username],
            "messages": [],
            "created_by": username,
        })
    elif join:
        room_exists = rooms_collection.find_one({"_id": code})
        if not room_exists:
            flash("Room does not exist.")
            return redirect(url_for("home"))
        
        # Add user to the room's user list only if they're not already in it
        rooms_collection.update_one(
            {"_id": code},
            {"$addToSet": {"users": username}}
        )
    
    session["room"] = room
    session["name"] = username
    
    # Update user's current room and rooms list
    users_collection.update_one(
        {"username": username},
        {
            "$set": {"current_room": room},
            "$addToSet": {"rooms": room}
        }
    )
    
    return redirect(url_for("room"))

@app.route("/room_settings/<room_code>")
@login_required
def room_settings(room_code):
    username = current_user.username
    room_data = rooms_collection.find_one({"_id": room_code})
    
    if not room_data:
        flash("Room does not exist.")
        return redirect(url_for("home"))
    
    # Check if user is in the room
    if username not in room_data["users"]:
        flash("You don't have access to this room.")
        return redirect(url_for("home"))
    
    # Get detailed user information for each room member
    room_users = []
    for user_name in room_data["users"]:
        user_data = users_collection.find_one({"username": user_name})
        if user_data:
            room_users.append({
                "username": user_name,
                "online": user_data.get("online", False),
                "current_room": user_data.get("current_room")
            })
    
    return render_template("room_settings.html",
                         room_code=room_code,
                         room_data=room_data,
                         room_users=room_users,
                         current_user=current_user,
                         is_owner=(room_data["created_by"] == username))

@app.route("/search_messages/<room_code>")
@login_required
def search_messages(room_code):
    username = current_user.username
    query = request.args.get('q', '').strip().lower()
    
    if not query:
        return jsonify({"messages": []})
    
    room_data = rooms_collection.find_one({"_id": room_code})
    
    if not room_data or username not in room_data["users"]:
        return jsonify({"error": "Access denied"}), 403
    
    # Search messages
    matching_messages = [
        msg for msg in room_data["messages"]
        if query in msg.get("message", "").lower()
    ]
    
    return jsonify({"messages": matching_messages[-50:]})  # Return last 50 matches

@app.route("/kick_user/<room_code>/<username>", methods=['POST'])
@login_required
def kick_user(room_code, username):
    current_username = current_user.username
    room_data = rooms_collection.find_one({"_id": room_code})
    
    if not room_data:
        return jsonify({"success": False, "message": "Room not found"}), 404
    
    if room_data["created_by"] != current_username:
        return jsonify({"success": False, "message": "Only room owner can kick users"}), 403
    
    if username == current_username:
        return jsonify({"success": False, "message": "You cannot kick yourself"}), 400
        
    if username == room_data["created_by"]:
        return jsonify({"success": False, "message": "Cannot kick room owner"}), 400
    
    if username not in room_data["users"]:
        return jsonify({"success": False, "message": "User not in room"}), 404
    
    # Remove user from room
    rooms_collection.update_one(
        {"_id": room_code},
        {"$pull": {"users": username}}
    )
    
    # Update kicked user's data
    users_collection.update_one(
        {"username": username},
        {
            "$pull": {"rooms": room_code},
            "$set": {"current_room": None}
        }
    )
    
    # Emit socket event to notify kicked user
    socketio.emit('user_kicked', {"room": room_code}, room=username)
    
    return jsonify({"success": True, "message": "User kicked successfully"})

def get_room_data(room_code):
    """Get room data from MongoDB"""
    try:
        room_data = rooms_collection.find_one({"_id": room_code})
        if not room_data:
            return None
        
        # Ensure all required fields exist
        room_data.setdefault("users", [])
        room_data.setdefault("messages", [])
        room_data.setdefault("created_by", "Unknown")
        
        return room_data
        
    except Exception as e:
        return None

@app.route("/room/", defaults={'code': None})
@app.route("/room/<code>")
@login_required
def room(code):
    username = current_user.username
    
    if code is None:
        code = session.get("room")
        if code is None:
            flash("No room code provided")
            return redirect(url_for("home"))
    
    room_data = rooms_collection.find_one({"_id": code})
    if not room_data:
        flash("Room does not exist")
        return redirect(url_for("home"))

    session["room"] = code
    session["name"] = username
    
    try:
        user_data = users_collection.find_one({"username": username})
        
        users_collection.update_one(
            {"username": username},
            {"$set": {"current_room": code}}
        )
        
        room_data.setdefault("users", [])
        room_data.setdefault("messages", [])
        room_data.setdefault("created_by", "")
        room_data.setdefault("name", "Unnamed Room")

        user_friends = set(user_data.get("friends", []))
        for message in room_data["messages"]:
            message["is_friend"] = message["name"] in user_friends
        
        user_list = []
        for user in room_data["users"]:
            user_profile = users_collection.find_one({"username": user})
            if user_profile:
                user_list.append({
                    "username": user,
                    "online": user_profile.get("online", False),
                    "isFriend": user in user_friends
                })

        friends_data = []
        for friend in user_friends:
            friend_data = users_collection.find_one({"username": friend})
            if friend_data:
                # Get room name if friend is in a room
                room_name = "Unknown Room"
                if friend_data.get("current_room"):
                    friend_room_data = get_room_data(friend_data["current_room"])
                    if friend_room_data:
                        room_name = friend_room_data.get("name", "Unnamed Room")
                
                friends_data.append({
                    "username": friend,
                    "online": friend_data.get("online", False),
                    "current_room": friend_data.get("current_room"),
                    "room_name": room_name
                })
        
        return render_template("room.html",
                            code=code,
                            room_name=room_data["name"],
                            messages=room_data["messages"],
                            users=user_list,
                            username=username,
                            created_by=room_data["created_by"],
                            friends=friends_data,
                            room_data=room_data)
                            
    except Exception as e:
        flash("Error loading room data")
        return redirect(url_for("home"))
    
@app.route("/exit_room/<code>")
@login_required
def exit_room(code):
    username = current_user.username
    user_data = users_collection.find_one({"username": username})
    
    # Verify the room exists
    room_data = rooms_collection.find_one({"_id": code})
    if not room_data:
        flash("Room does not exist.")
        return redirect(url_for("home"))
    
    # Verify user is not the room owner
    if room_data["created_by"] == username:
        flash("Room owners cannot leave their own rooms. You must delete the room instead.")
        return redirect(url_for("home"))
    
    # Update user data
    result = users_collection.update_one(
        {"username": username},
        {
            "$pull": {"rooms": code},
            "$set": {"current_room": None}
        }
    )
    
    # Always remove the user from the room's user list when exiting
    rooms_collection.update_one(
        {"_id": code},
        {"$pull": {"users": username}}
    )
    
    flash("You have left the room successfully.")
    return redirect(url_for("home"))
    
@app.route("/update_room_name/<room_code>", methods=['POST'])
@login_required
def update_room_name(room_code):
    username = current_user.username
    new_name = request.form.get('room_name', '').strip()
    
    if not new_name:
        flash("Room name cannot be empty.")
        return redirect(url_for("room", code=room_code))
    
    room_data = rooms_collection.find_one({f"_id": room_code})
    
    if not room_data:
        flash("Room does not exist.")
        return redirect(url_for("home"))
    
    if room_data["created_by"] != username:
        flash("You don't have permission to update this room's name.")
        return redirect(url_for("room", code=room_code))
    
    # Update room name
    rooms_collection.update_one(
        {"_id": room_code},
        {"$set": {"name": new_name}}
    )
    
    flash("Room name updated successfully.")
    return redirect(url_for("room", code=room_code))

@socketio.on("toggle_reaction")
def handle_reaction(data):
    room = session.get("room")
    username = current_user.username
    if not room or not username:
        return

    message_id = data["messageId"]
    emoji = data["emoji"]

    # Check if the user has already reacted with this emoji
    message = rooms_collection.find_one(
        {
            "_id": room,
            "messages.id": message_id
        },
        {"messages.$": 1}
    )

    if not message or not message.get("messages"):
        return

    current_message = message["messages"][0]
    current_reactions = current_message.get("reactions", {})
    current_emoji_data = current_reactions.get(emoji, {"count": 0, "users": []})

    if username in current_emoji_data.get("users", []):
        # Remove user's reaction
        rooms_collection.update_one(
            {"_id": room, "messages.id": message_id},
            {
                "$pull": {f"messages.$[msg].reactions.{emoji}.users": username},
                "$inc": {f"messages.$[msg].reactions.{emoji}.count": -1}
            },
            array_filters=[{"msg.id": message_id}]
        )

        # Clean up empty reactions
        rooms_collection.update_one(
            {"_id": room, "messages.id": message_id},
            {
                "$unset": {f"messages.$[msg].reactions.{emoji}": ""}
            },
            array_filters=[
                {"msg.id": message_id},
                {f"msg.reactions.{emoji}.count": 0}
            ]
        )
    else:
        # Add user's reaction
        rooms_collection.update_one(
            {"_id": room, "messages.id": message_id},
            {
                "$set": {
                    f"messages.$[msg].reactions.{emoji}": {
                        "count": current_emoji_data.get("count", 0) + 1,
                        "users": current_emoji_data.get("users", []) + [username]
                    }
                }
            },
            array_filters=[{"msg.id": message_id}]
        )

    # Emit updated reactions to all users in the room
    updated_message = rooms_collection.find_one(
        {"_id": room, "messages.id": message_id},
        {"messages.$": 1}
    )
    
    if updated_message and updated_message.get("messages"):
        emit("update_reactions", {
            "messageId": message_id,
            "reactions": updated_message["messages"][0].get("reactions", {})
        }, room=room)



@socketio.on("message")
def message(data):
    room = session.get("room")
    room_data = rooms_collection.find_one({"_id": room})
    if not room or not room_data:
        return 

    # Handle reply_to data structure
    reply_to = None
    if data.get("replyTo"):
        if isinstance(data["replyTo"], dict):
            reply_to = {
                "id": data["replyTo"]["id"],
                "message": data["replyTo"]["message"]
            }
        else:
            # If we only got an ID, try to find the message content
            original_message = rooms_collection.find_one(
                {"_id": room, "messages.id": data["replyTo"]},
                {"messages.$": 1}
            )
            if original_message and original_message.get("messages"):
                reply_to = {
                    "id": data["replyTo"],
                    "message": original_message["messages"][0]["message"]
                }

    content = {
        "id": str(ObjectId()),
        "name": current_user.username,
        "message": data["data"],
        "reply_to": reply_to,  # Store the complete reply information
        "read_by": [session.get("username")],
        "image": data.get("image"),
        "reactions": {}
    }
    
    rooms_collection.update_one(
        {"_id": room},
        {"$push": {"messages": content}}
    )

    send(content, to=room)

    # Send push notification to all users in the room except the sender
    sender_username = current_user.username
    room_users = room_data["users"]
    
    # Prepare notification content
    notification_content = {
        'sender': sender_username,
        'message': content['message'],
        'room_id': str(room),
        'room_name': room_data.get('name', 'Group Chat')
    }
    
    for username in room_users:
        if username != sender_username:
            # Get user's notification settings
            user_data = users_collection.find_one(
                {"username": username},
                {"notification_settings": 1, "fcm_token": 1}
            )
            
            if not user_data or "fcm_token" not in user_data:
                continue
                
            settings = user_data.get("notification_settings", {})
            
            # Determine if we should send notification based on room type
            should_notify = False
            if len(room_users) == 2 and settings.get("direct_messages", True):
                should_notify = True
            elif len(room_users) > 2 and settings.get("group_messages", True):
                should_notify = True
                
            # Check if notifications are enabled globally
            if should_notify and settings.get("enabled", False):
                try:
                    notification_type = 'direct_message' if len(room_users) == 2 else 'group_message'
                    message = messaging.Message(
                        notification=messaging.Notification(
                            title=f"New message from {content['name']}",
                            body=content['message'][:100] + ('...' if len(content['message']) > 100 else '')
                        ),
                        data={
                            'type': notification_type,
                            'room_id': str(room),
                            'sender': sender_username
                        },
                        token=user_data["fcm_token"]
                    )
                    messaging.send(message)
                except Exception as e:
                    print(f'Error sending notification to {username}:', e)

@socketio.on("load_more_messages")
def load_more_messages(data):
    room = session.get("room")
    last_message_id = data.get("last_message_id")
    
    room_data = rooms_collection.find_one({"_id": room})
    if not room_data:
        return
    
    all_messages = room_data.get("messages", [])
    
    if not last_message_id:
        return
    
    last_message_index = next((i for i, msg in enumerate(all_messages) if msg["id"] == last_message_id), None)
    
    if last_message_index is None:
        return
    
    # Load 20 more messages before the last loaded message
    start_index = max(0, last_message_index - 20)
    messages_to_send = all_messages[start_index:last_message_index]
    
    messages_with_read_status = []
    for msg in messages_to_send:
        msg_copy = msg.copy()
        msg_copy["read_by"] = msg_copy.get("read_by", [])
        for key, value in msg_copy.items():
            if isinstance(value, datetime):
                msg_copy[key] = datetime_to_iso(value)
        messages_with_read_status.append(msg_copy)
    
    socketio.emit("more_messages", {
        "messages": messages_with_read_status,
        "has_more": start_index > 0
    }, room=request.sid)

@socketio.on("connect")
def connect():
    room = session.get("room")
    username = current_user.username
    if not room or not username:
        return
    
    join_room(room)
    
    # Update user's current room and rooms list
    users_collection.update_one(
        {"username": username},
        {
            "$set": {"current_room": room},
            "$addToSet": {"rooms": room}
        }
    )
    
    # Add user to the room's user list if not already present
    rooms_collection.update_one(
        {"_id": room},
        {"$addToSet": {"users": username}}
    )
    
    # Get updated room data
    room_data = rooms_collection.find_one({"_id": room})
    user_data = users_collection.find_one({"username": username})
    
    # Send updated user list with online status and friend information
    user_list = []
    for user in room_data["users"]:
        user_profile = users_collection.find_one({"username": user})
        user_list.append({
            "username": user,
            "online": user_profile.get("online", False),
            "isFriend": user in user_data.get("friends", [])
        })
    
    socketio.emit("update_users", {
        "users": user_list,
        "room_name": room_data.get("name", "Unnamed Room")  # Send room name
    }, room=room)
    
    # Load only the most recent 20 messages
    messages = room_data.get("messages", [])[-20:]
    messages_with_read_status = []
    for msg in messages:
        msg_copy = msg.copy()
        msg_copy["read_by"] = msg_copy.get("read_by", [])
        for key, value in msg_copy.items():
            if isinstance(value, datetime):
                msg_copy[key] = datetime_to_iso(value)
        messages_with_read_status.append(msg_copy)

    socketio.emit("chat_history", {
        "messages": messages_with_read_status,
        "has_more": len(messages) < len(room_data.get("messages", [])),
        "room_name": room_data.get("name", "Unnamed Room")  # Send room name
    }, room=request.sid)

@socketio.on("disconnect")
def disconnect():
    username = current_user.username
    room = session.get("room")
    
    if not username or not room:
        return
        
    leave_room(room)
    
    # Update user profile
    users_collection.update_one(
        {"username": username},
        {"$set": {"current_room": None}}
    )
    
    # Note: We no longer remove the user from the room's user list here
    
    # Get updated room data and notify remaining users
    room_data = rooms_collection.find_one({"_id": room})
    user_list = []
    for user in room_data["users"]:
        user_profile = users_collection.find_one({"username": user})
        user_list.append({
            "username": user,
            "online": user_profile.get("online", False),
            "isFriend": False
        })
    socketio.emit("update_users", {"users": user_list}, room=room)
                
@app.route("/get_unread_messages")
@login_required
def fetch_unread_messages():
    username = current_user.username
    if not username:
        return jsonify({"error": "User not logged in"}), 401
    
    unread_messages = get_unread_messages(username)
    return jsonify(unread_messages)

def get_unread_messages(username):
    # Get the user's data
    user = users_collection.find_one({"username": username})
    if not user:
        return {"error": "User not found"}

    # Get all rooms the user is in
    user_rooms = rooms_collection.find({"users": username})

    unread_messages = {}

    for room in user_rooms:
        room_id = str(room["_id"])
        unread_count = 0
        unread_msg_details = []

        for message in room["messages"]:
            # Check if the message is not read by the user and not sent by the user
            if username not in message.get("read_by", []) and message["name"] != username:
                unread_count += 1
                unread_msg_details.append({
                    "id": message["id"],
                    "sender": message["name"],
                    "content": message.get("message", "Image message" if "image" in message else "Unknown content"),
                })

        if unread_count > 0:
            unread_messages[room_id] = {
                "unread_count": unread_count,
                "messages": unread_msg_details
            }

    return unread_messages

@socketio.on("mark_messages_read")
def mark_messages_read(data):
    room = session.get("room")
    username = current_user.username
    if not room or not username:
        return

    current_time = datetime.utcnow()

    # Update the read status of messages in the room
    rooms_collection.update_many(
        {
            "_id": room,
            "messages": {
                "$elemMatch": {
                    "id": {"$in": data["message_ids"]},
                    "read_by": {"$ne": username}
                }
            }
        },
        {
            "$addToSet": {
                "messages.$[elem].read_by": username
            },
        },
        array_filters=[{"elem.id": {"$in": data["message_ids"]}}]
    )

    # Emit an event to notify other users that messages have been read
    socketio.emit("messages_read", {
        "reader": username,
        "message_ids": data["message_ids"],
    }, room=room)

@socketio.on("edit_message")
def edit_message(data):
    room = session.get("room")
    name = session.get("name")
    if not room:
        return

    # Update message in MongoDB - fixed array element matching
    result = rooms_collection.update_one(
        {
            "_id": room,
            "messages": {
                "$elemMatch": {
                    "id": data["messageId"],
                    "name": name  # Ensure only message owner can edit
                }
            }
        },
        {
            "$set": {
                "messages.$[elem].message": data["newText"],
                "messages.$[elem].edited": True
            }
        },
        array_filters=[{"elem.id": data["messageId"]}]
    )
    
    if result.modified_count:
        socketio.emit("edit_message", {
            "messageId": data["messageId"],
            "newText": data["newText"]
        }, room=room)

@socketio.on("delete_message")
def delete_message(data):
    room = session.get("room")
    name = session.get("name")
    if not room:
        return

    # Remove message from MongoDB
    result = rooms_collection.update_one(
        {"_id": room},
        {
            "$pull": {
                "messages": {
                    "id": data["messageId"],
                    "name": name
                }
            }
        }
    )
    
    if result.modified_count:
        socketio.emit("delete_message", {"messageId": data["messageId"]}, room=room)
        
@socketio.on("typing")
def handle_typing(data):
    room = session.get("room")
    if room:
        name = session.get("name")
        socketio.emit("typing", {"name": name, "isTyping": data.get("isTyping", False)}, room=room, include_self=False)

@app.teardown_appcontext
def shutdown_scheduler(exception=None):
    if scheduler.running:
        scheduler.shutdown()

if __name__ == "__main__":
    # Create indexes only if they don't exist
    existing_indexes = users_collection.index_information()

    if "username_1" not in existing_indexes:
        users_collection.create_index([("username", 1)], unique=True)
    
    if "users_1" not in rooms_collection.index_information():
        rooms_collection.create_index([("users", 1)])
    
    if "messages.id_1" not in rooms_collection.index_information():
        rooms_collection.create_index([("messages.id", 1)])
    
    port = int(os.environ.get("PORT", 5002))
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True, host='0.0.0.0', port=port)