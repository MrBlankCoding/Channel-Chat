# Standard library imports
import os
import random
import re
import io
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
import urllib.parse
import string
import tempfile
import mimetypes

# Third-party library imports
from asgiref.wsgi import WsgiToAsgi
import requests
import pytz
from flask import (
    Flask,
    render_template,
    request,
    session,
    redirect,
    url_for,
    send_from_directory,
    flash,
    jsonify,
)
from flask_socketio import join_room, leave_room, send, emit, SocketIO
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from firebase_admin import credentials, messaging, storage
import firebase_admin
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from pymongo import MongoClient
from fuzzywuzzy import fuzz
from bson import ObjectId
import ffmpeg
from PIL import Image

load_dotenv()

cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred, {"storageBucket": "channelchat-7d679.appspot.com"})

app = Flask(__name__)
asgi_app = WsgiToAsgi(app)
scheduler = BackgroundScheduler()


@app.context_processor
def utility_processor():
    return dict(get_room_data=get_room_data)


app.secret_key = os.getenv("SECRET_KEY")
app.config["ALLOWED_IMAGE_TYPES"] = {"png", "jpeg", "jpg", "gif"}

# Initialize MongoDB client using the URI from .env
client = MongoClient(os.getenv("MONGO_URI"))
db = client["chat_app_db"]

# Collections
users_collection = db["users"]
rooms_collection = db["rooms"]
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
login_manager.login_view = "login"

MAX_VIDEO_SIZE_MB = 50
ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
}
VIDEO_COMPRESS_CRF = 28  # Compression quality (0-51, lower is better quality)


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
socketio = SocketIO(app, cors_allowed_origins="*")


def datetime_to_iso(dt):
    return dt.isoformat() if dt else None


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_IMAGE_TYPES"]
    )

@app.route("/notification-settings", methods=["GET", "POST"])
@login_required
def notification_settings():
    if request.method == "GET":
        user = users_collection.find_one(
            {"username": current_user.username}, {"notification_settings": 1}
        )
        settings = user.get("notification_settings", {"enabled": False})
        return jsonify(settings)

    elif request.method == "POST":
        try:
            settings = request.json
            users_collection.update_one(
                {"username": current_user.username},
                {"$set": {"notification_settings": settings}},
            )
            return jsonify({"message": "Settings updated successfully"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400


@app.route("/register-fcm-token", methods=["POST"])
@login_required
def register_fcm_token():
    try:
        data = request.json
        fcm_token = data.get("token")
        clear_all = data.get("clearAll", False)

        if clear_all:
            # Clear the FCM token
            users_collection.update_one(
                {"username": current_user.username},
                {
                    "$unset": {
                        "fcm_token": "",
                        "notification_settings.enabled": "",
                    }
                },
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
                    "notification_settings.enabled": True,
                }
            },
        )

        return jsonify({"message": "FCM token registered successfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400



def send_notification(recipient_username, sender_username, message_text):
    recipient = users_collection.find_one({"username": recipient_username})

    if not recipient:
        return False

    settings = recipient.get("notification_settings", {})
    fcm_token = recipient.get("fcm_token")

    # Only send notification if enabled
    if not settings.get("enabled") or not fcm_token:
        return False

    try:
        # Build the message
        message = messaging.Message(
            notification=messaging.Notification(
                title=f"New message from {sender_username}",
                body=message_text[:100] + ("..." if len(message_text) > 100 else ""),
            ),
            token=fcm_token,
            android=messaging.AndroidConfig(
                notification=messaging.AndroidNotification(
                    sound="default"  # Default sound on Android
                )
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default"  # Default sound on iOS
                    )
                )
            )
        )

        messaging.send(message)
        return True

    except Exception as e:
        print(f"Error sending notification to {recipient_username}: {e}")
        return False

@app.route("/firebase-messaging-sw.js")
def serve_sw():
    root_dir = os.path.abspath(
        os.getcwd()
    )  # Gets the current working directory (project root)
    return send_from_directory(
        root_dir, "firebase-messaging-sw.js", mimetype="application/javascript"
    )


@app.route("/update_profile_photo/<username>", methods=["POST"])
@login_required
def update_profile_photo(username):
    # Check if photo is included in the request
    if "photo" not in request.files:
        return jsonify({"error": "No photo provided"}), 400

    photo = request.files["photo"]
    if photo.filename == "":
        return jsonify({"error": "No photo selected"}), 400

    # Verify allowed file extensions by checking the filename
    if not allowed_file(photo.filename):
        return jsonify({"error": "Invalid file type"}), 400

    try:
        # Open the image with Pillow to ensure it is a valid image
        image = Image.open(photo)

        # Verify file format (JPEG, PNG, GIF)
        allowed_types = app.config["ALLOWED_IMAGE_TYPES"]
        if image.format.lower() not in allowed_types:
            return (
                jsonify(
                    {
                        "error": f"Invalid image type. Allowed types: {', '.join(allowed_types).upper()}"
                    }
                ),
                400,
            )

        # Check file size (max 5MB)
        photo.seek(0, io.SEEK_END)
        file_size = photo.tell()
        if file_size > 5 * 1024 * 1024:
            return (
                jsonify({"error": "File too large. Maximum size is 5MB"}),
                400,
            )
        photo.seek(0)  # Reset file pointer for reading

        # Get existing photo URL from user document if it exists
        user_data = users_collection.find_one({"username": username})
        if not user_data:
            return jsonify({"error": "User not found"}), 404

        existing_photo_url = user_data.get("profile_photo")
        if existing_photo_url:
            try:
                delete_firebase_image(existing_photo_url)
            except Exception as e:
                print(f"Error deleting existing profile photo: {e}")

        # Resize the image to 200x200 pixels maximum
        image.thumbnail((200, 200))

        # Save the resized image to a BytesIO object in the appropriate format
        img_io = io.BytesIO()
        image.save(img_io, format=image.format)
        img_io.seek(0)

        # Generate filename using username
        filename = f"profile_photos/{username}.{image.format.lower()}"

        # Upload to Firebase Storage
        bucket = storage.bucket()
        blob = bucket.blob(filename)
        blob.content_type = f"image/{image.format.lower()}"
        blob.upload_from_file(img_io, content_type=blob.content_type)

        # Make the image publicly accessible
        blob.make_public()

        # Get the public URL
        photo_url = blob.public_url

        # Update user document with the new photo URL
        users_collection.update_one(
            {"username": username}, {"$set": {"profile_photo": photo_url}}
        )

        return jsonify({"photo_url": photo_url}), 200

    except Exception as e:
        print(f"Error uploading profile photo: {e}")
        return jsonify({"error": "Failed to upload photo"}), 500


@app.route("/profile_photos/<username>")
def profile_photo(username):
    """Serve the user's profile photo from Firebase Cloud Storage"""
    for ext in app.config["ALLOWED_IMAGE_TYPES"]:
        filename = f"profile_photos/{username}.{ext}"
        blob = storage.bucket().blob(filename)

        try:
            exists = blob.exists()
            if exists:
                return redirect(blob.public_url)
        except Exception as e:
            print(f"Error checking blob existence: {e}")

    # If no profile photo is found, return the default profile image
    return redirect(url_for("default_profile"))


@app.route("/default-profile")
def default_profile():
    # Serve the default profile image if no custom image exists
    return send_from_directory("static/images", "default-profile.png")


# Set up the background scheduler
def check_inactive_users():
    threshold = datetime.utcnow() - timedelta(minutes=5)
    inactive_users = heartbeats_collection.find({"last_heartbeat": {"$lt": threshold}})

    for user in inactive_users:
        users_collection.update_one(
            {"username": user["username"]}, {"$set": {"online": False}}
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
        upsert=True,
    )
    users_collection.update_one({"username": username}, {"$set": {"online": True}})
    return "", 204


@app.route("/stop_heartbeat", methods=["POST"])
@login_required
def stop_heartbeat():
    username = current_user.username
    heartbeats_collection.delete_one({"username": username})
    users_collection.update_one({"username": username}, {"$set": {"online": False}})
    return "", 204


def generate_unique_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if not rooms_collection.find_one({"_id": code}):
            return code


def get_user_data(username):
    """Get user data from MongoDB"""
    user_data = users_collection.find_one({"username": username})
    if user_data:
        # Convert ObjectId to string for JSON serialization
        user_data["_id"] = str(user_data["_id"])
        # Ensure room_invites exists
        if "room_invites" not in user_data:
            user_data["room_invites"] = []
            users_collection.update_one(
                {"username": username}, {"$set": {"room_invites": []}}
            )
    return user_data


# Helper function to update user data
def update_user_data(username, data):
    """Update user data in MongoDB"""
    if "_id" in data:
        del data["_id"]  # Remove _id if present to avoid update errors
    users_collection.update_one({"username": username}, {"$set": data})


# Flask route
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        # Input validation
        if not username or not password:
            flash("Username and password are required!")
            return redirect(url_for("register"))

        # Validate username format
        if not re.match(r"^[a-zA-Z0-9_.-]+$", username):
            flash(
                "Username can only contain letters, numbers, dots, underscores, and hyphens!"
            )
            return redirect(url_for("register"))

        # Validate password requirements
        if len(password) < 8:
            flash("Password must be at least 8 characters long!")
            return redirect(url_for("register"))
        if not re.search(r"[a-zA-Z]", password):
            flash("Password must contain at least one letter!")
            return redirect(url_for("register"))
        if not re.search(r"\d", password):
            flash("Password must contain at least one number!")
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
            "rooms": [],
        }

        try:
            users_collection.insert_one(user_data)
            flash("Registration successful! Please login.")
            return redirect(url_for("login"))
        except Exception as e:
            app.logger.error(f"Registration error: {str(e)}")
            flash("An error occurred during registration. Please try again.")
            return redirect(url_for("register"))

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
            upsert=True,
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
    users_collection.update_one({"username": username}, {"$set": {"online": False}})

    logout_user()
    flash("You have been logged out.")
    return redirect(url_for("login"))


@app.route("/delete_account", methods=["POST"])
@login_required
def delete_account():
    username = current_user.username

    # Get user data to check for profile photo
    user_data = users_collection.find_one({"username": username})
    if user_data and user_data.get("profile_photo"):
        try:
            # Delete user's profile photo from Firebase Storage
            profile_photo_url = user_data["profile_photo"]
            # Extract the file extension from the URL
            ext = profile_photo_url.split(".")[-1].lower()
            profile_photo_path = f"profile_photos/{username}.{ext}"

            bucket = storage.bucket()
            blob = bucket.blob(profile_photo_path)

            if blob.exists():
                blob.delete()
        except Exception as e:
            print(f"Failed to delete user profile photo: {str(e)}")

    # Delete all rooms created by the user
    rooms_to_delete = rooms_collection.find({"created_by": username})

    deleted_images = 0
    failed_deletions = 0

    for room in rooms_to_delete:
        room_code = room["_id"]

        # Delete room profile photo if it exists
        if room.get("profile_photo"):
            try:
                ext = room["profile_photo"].split(".")[-1]
                profile_photo_path = f"room_profile_photos/{room_code}.{ext}"

                bucket = storage.bucket()
                blob = bucket.blob(profile_photo_path)

                if blob.exists():
                    blob.delete()
                    deleted_images += 1
            except Exception as e:
                failed_deletions += 1
                print(f"Failed to delete room profile photo: {str(e)}")

        # Delete message images
        if "messages" in room:
            for message in room["messages"]:
                if "image" in message and message["image"]:
                    try:
                        image_path = message["image"].split("/o/")[1].split("?")[0]
                        image_path = urllib.parse.unquote(image_path)

                        blob = storage.bucket().blob(image_path)
                        if blob.exists():
                            blob.delete()
                            deleted_images += 1
                    except Exception as e:
                        failed_deletions += 1
                        print(
                            f"Failed to delete image for message: {message.get('id', 'unknown')}"
                        )
                        print(f"Error: {str(e)}")

        # Remove room from all users who are in it
        users_collection.update_many(
            {"rooms": room_code},
            {"$pull": {"rooms": room_code}, "$set": {"current_room": None}},
        )

    # Delete all rooms created by the user
    rooms_collection.delete_many({"created_by": username})

    # Remove user from all rooms they are part of
    rooms_collection.update_many(
        {"members": username}, {"$pull": {"members": username}}
    )

    # Remove friend relationships
    users_collection.update_many(
        {"friends": username}, {"$pull": {"friends": username}}
    )

    # Remove any pending friend requests sent by the user
    users_collection.update_many(
        {"friend_requests": username}, {"$pull": {"friend_requests": username}}
    )

    # Remove any pending friend requests received by the user
    users_collection.update_one(
        {"username": username}, {"$set": {"friend_requests": []}}
    )

    # Delete user's heartbeat entry if present
    heartbeats_collection.delete_one({"username": username})

    # Delete user from the users collection
    users_collection.delete_one({"username": username})

    # Log out the user after account deletion
    logout_user()

    if deleted_images > 0 or failed_deletions > 0:
        flash(
            f"Account deleted. Successfully removed {deleted_images} images. {failed_deletions} images failed to delete."
        )
    else:
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

        # Call update_profile_photo and unpack the response and status code
        if profile_photo:
            with app.test_request_context(
                f"/update_profile_photo/{username}",
                method="POST",
                data={"photo": profile_photo},
            ):
                response, status_code = update_profile_photo(username)
                if status_code == 200:
                    flash("Profile photo updated successfully!")
                else:
                    flash(
                        response.json.get(
                            "error",
                            "Failed to upload profile photo. Please try again.",
                        )
                    )

        # Validate current password
        if current_password and not check_password_hash(
            user_data["password"], current_password
        ):
            flash("Current password is incorrect!")
            return redirect(url_for("settings"))

        # Update password
        if new_password:
            # Inline password strength check
            if (
                len(new_password) < 8
                or not any(char.isdigit() for char in new_password)
                or not any(char.isalpha() for char in new_password)
            ):
                flash(
                    "Password must be at least 8 characters long and include letters and numbers."
                )
                return redirect(url_for("settings"))

            if new_password != confirm_new_password:
                flash("New passwords do not match!")
                return redirect(url_for("settings"))

            users_collection.update_one(
                {"username": username},
                {"$set": {"password": generate_password_hash(new_password)}},
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
        {"$addToSet": {"friend_requests": username}},
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
        flash(
            "This user has already sent you a friend request! Check your friend requests to accept it."
        )
        return redirect(url_for("home"))

    # Add friend request
    users_collection.update_one(
        {"username": friend_username},
        {"$addToSet": {"friend_requests": username}},
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
        {"username": current_username, "friend_requests": username},
        {
            "$pull": {"friend_requests": username},
            "$addToSet": {"friends": username},
        },
    )

    if result.modified_count:
        users_collection.update_one(
            {"username": username}, {"$addToSet": {"friends": current_username}}
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
        {
            "username": current_username
        },  # Use the actual username, not the LocalProxy object
        {"$pull": {"friend_requests": username}},
    )

    if result.modified_count:
        flash(f"Friend request from {username} declined.")
    else:
        flash("No friend request found!")

    return redirect(url_for("home"))


@app.route("/remove_friend/<username>", methods=["POST"])
@login_required
def remove_friend(username):
    current_username = (
        current_user.username
    )  # Access the username of the current_user object

    # Remove from both users' friend lists atomically
    result = users_collection.update_one(
        {
            "username": current_username,  # Use the extracted username
            "friends": username,
        },
        {"$pull": {"friends": username}},
    )

    if result.modified_count:
        users_collection.update_one(
            {"username": username},
            {
                "$pull": {"friends": current_username}
            },  # Use the extracted username here as well
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
            "current_room": None,
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
            friends_data.append(
                {
                    "username": friend,
                    "online": friend_data.get("online", False),
                    "current_room": friend_data.get("current_room"),
                }
            )

    return render_template(
        "homepage.html",
        username=username,
        user_data=user_data,
        friends=friends_data,
        friend_requests=user_data.get("friend_requests", []),
    )


@app.route("/search_users", methods=["GET"])
@login_required
def search_users():
    query = request.args.get("q", "").lower().strip()
    if not query:
        return jsonify([])

    # Get current user's data for exclusion list
    current_user_data = users_collection.find_one({"username": current_user.username})
    friends_list = current_user_data.get("friends", [])

    # First, get all eligible users (excluding current user and friends)
    all_users = list(
        users_collection.find(
            {
                "username": {"$ne": current_user.username},
                "username": {"$nin": friends_list},
            },
            {
                "username": 1
            },  # We don't need to fetch profile_photo field since we're using the route
        )
    )

    # If it's just one character, only match first letter
    if len(query) == 1:
        matching_users = [
            user for user in all_users if user["username"].lower().startswith(query)
        ]
    else:
        # For longer queries, use fuzzy matching
        username_matches = [
            (user, fuzz.ratio(query, user["username"].lower())) for user in all_users
        ]

        # Filter based on different criteria depending on query length
        if len(query) <= 3:
            matching_users = [
                user
                for user, score in username_matches
                if score > 50 or user["username"].lower().startswith(query)
            ]
        else:
            matching_users = [user for user, score in username_matches if score > 70]

        # Sort by similarity score
        matching_users.sort(
            key=lambda x: fuzz.ratio(query, x["username"].lower()), reverse=True
        )

    # Limit results
    matching_users = matching_users[:5]

    # Format response using the profile_photo route
    suggestions = [
        {
            "username": user["username"],
            "profile_photo_url": url_for(
                "profile_photo", username=user["username"], _external=True
            ),
            "similarity": fuzz.ratio(query, user["username"].lower()),
        }
        for user in matching_users
    ]

    return jsonify(suggestions)


def delete_firebase_image(image_url):
    """Helper function to delete an image from Firebase Storage"""
    if not image_url:
        return

    try:
        # Extract the path from the Firebase Storage URL, similar to JS implementation
        # URL format: https://firebasestorage.googleapis.com/v0/b/[bucket]/o/[path]?token...
        path = image_url.split("/o/")[1].split("?")[0]
        # URL decode the path to handle special characters and spaces
        path = urllib.parse.unquote(path)

        # Get bucket and create blob reference
        bucket = storage.bucket()
        blob = bucket.blob(path)

        # Delete the blob
        blob.delete()
    except Exception as e:
        print(f"Error deleting image from Firebase Storage: {e}")
        print(f"Attempted to delete path: {path}")


@app.route("/pending_room_invites")
def pending_room_invites():
    if not current_user:
        flash("Please login to view pending invites.")
        return redirect(url_for("login"))

    # Get current user's data
    current_username = current_user.username
    user_data = get_user_data(current_username)

    # Initialize pending_invites if it doesn't exist
    if "pending_invites" not in user_data:
        user_data["pending_invites"] = []

    return render_template("room_invites.html", user_data=user_data)


@app.route("/cancel_room_invite/<username>/<room_code>")
def cancel_room_invite(username, room_code):
    if not current_user:
        flash("Please login to cancel invites.")
        return redirect(url_for("login"))

    # Get the invited user's data
    friend_data = get_user_data(username)
    if not friend_data:
        flash("User not found.")
        return redirect(url_for("room"))

    # Remove the invite from their room_invites
    if "room_invites" in friend_data:
        friend_data["room_invites"] = [
            inv for inv in friend_data["room_invites"] if inv.get("room") != room_code
        ]
        update_user_data(username, friend_data)

    # Remove from current user's pending_invites
    current_username = current_user.username
    user_data = get_user_data(current_username)
    if "pending_invites" in user_data:
        user_data["pending_invites"] = [
            inv
            for inv in user_data["pending_invites"]
            if inv.get("username") != username or inv.get("room") != room_code
        ]
        update_user_data(current_username, user_data)

    flash(f"Cancelled room invitation to {username}.")
    return redirect(url_for("room"))


# Modified invite_to_room route to include pending_invites
@app.route("/invite_to_room/<username>")
def invite_to_room(username):
    current_room = session.get("room")

    if not current_room:
        flash("You're not in a room.")
        return redirect(url_for("home"))

    # Get the room data
    room_data = rooms_collection.find_one({"_id": current_room})
    if not room_data:
        flash("Room not found.")
        return redirect(url_for("home"))

    # Get the friend's data
    friend_data = get_user_data(username)
    if not friend_data:
        flash("User not found.")
        return redirect(url_for("room"))

    # Get current user's data
    current_username = current_user.username
    user_data = get_user_data(current_username)

    if username not in user_data.get("friends", []):
        flash("You can only invite friends to rooms.")
        return redirect(url_for("room"))

    # Initialize room_invites for friend
    if "room_invites" not in friend_data:
        friend_data["room_invites"] = []

    # Initialize pending_invites for current user
    if "pending_invites" not in user_data:
        user_data["pending_invites"] = []

    # Check if invite already exists
    existing_invite = next(
        (inv for inv in friend_data["room_invites"] if inv.get("room") == current_room),
        None,
    )

    if not existing_invite:
        # Create new invite
        new_invite = {
            "room": current_room,
            "room_name": room_data.get("name", "Unnamed Room"),
            "from": current_username,
            "profile_photo": room_data.get("profile_photo"),
        }
        friend_data["room_invites"].append(new_invite)

        # Add to pending_invites for current user
        pending_invite = {
            "username": username,
            "room": current_room,
            "room_name": room_data.get("name", "Unnamed Room"),
            "profile_photo": room_data.get("profile_photo"),
        }
        user_data["pending_invites"].append(pending_invite)

        # Save both updated data
        update_user_data(username, friend_data)
        update_user_data(current_username, user_data)
        flash(f"Room invitation sent to {username}!")
    else:
        flash(f"{username} already has a pending invite to this room.")

    return redirect(url_for("room"))


@app.route("/accept_room_invite/<room_code>")
@login_required
def accept_room_invite(room_code):
    username = current_user.username
    user_data = get_user_data(username)

    # Find and remove the invite
    invite_found = False
    room_invites = user_data.get("room_invites", [])

    # Find the invite to get the sender's username before removing it
    invite = next((inv for inv in room_invites if inv["room"] == room_code), None)

    if not invite:
        flash("Room invite not found or already accepted.")
        return redirect(url_for("home"))

    # Get the sender's username from the invite
    sender_username = invite["from"]

    # Filter out the accepted invite from recipient
    user_data["room_invites"] = [
        inv for inv in room_invites if inv["room"] != room_code
    ]

    # Add room to user's rooms list
    if "rooms" not in user_data:
        user_data["rooms"] = []
    if room_code not in user_data["rooms"]:
        user_data["rooms"].append(room_code)

    # Save the updated recipient's data
    update_user_data(username, user_data)

    # Remove pending invite from sender's data
    sender_data = get_user_data(sender_username)
    if sender_data and "pending_invites" in sender_data:
        sender_data["pending_invites"] = [
            inv
            for inv in sender_data["pending_invites"]
            if not (inv["username"] == username and inv["room"] == room_code)
        ]
        update_user_data(sender_username, sender_data)

    flash("Room invite accepted!")
    return redirect(url_for("room", code=room_code))


@app.route("/decline_room_invite/<room_code>")
@login_required
def decline_room_invite(room_code):
    username = current_user.username
    user_data = get_user_data(username)

    # Find the invite to get the sender's username before removing it
    room_invites = user_data.get("room_invites", [])
    invite = next((inv for inv in room_invites if inv["room"] == room_code), None)

    if not invite:
        flash("Room invite not found or already declined.")
        return redirect(url_for("home"))

    # Get the sender's username from the invite
    sender_username = invite["from"]

    # Remove the invite from recipient
    user_data["room_invites"] = [
        inv for inv in room_invites if inv["room"] != room_code
    ]
    update_user_data(username, user_data)

    # Remove pending invite from sender's data
    sender_data = get_user_data(sender_username)
    if sender_data and "pending_invites" in sender_data:
        sender_data["pending_invites"] = [
            inv
            for inv in sender_data["pending_invites"]
            if not (inv["username"] == username and inv["room"] == room_code)
        ]
        update_user_data(sender_username, sender_data)

    flash("Room invite declined.")
    return redirect(url_for("home"))


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
        {"username": username}, {"$set": {"current_room": friend_room}}
    )

    return redirect(url_for("room"))


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

    # Delete all media from Firebase Storage
    deleted_items = 0
    failed_deletions = 0

    # Delete room profile photo
    if room_data.get("profile_photo"):
        try:
            ext = room_data["profile_photo"].split(".")[-1]
            profile_photo_path = f"room_profile_photos/{room_code}.{ext}"

            bucket = storage.bucket()
            blob = bucket.blob(profile_photo_path)

            if blob.exists():
                blob.delete()
                deleted_items += 1
        except Exception as e:
            failed_deletions += 1
            print(f"Failed to delete room profile photo: {str(e)}")

    # Delete message media (images and videos)
    if "messages" in room_data:
        for message in room_data["messages"]:
            # Handle images
            if "image" in message and message["image"]:
                try:
                    image_path = message["image"].split("/o/")[1].split("?")[0]
                    image_path = urllib.parse.unquote(image_path)

                    blob = storage.bucket().blob(image_path)
                    if blob.exists():
                        blob.delete()
                        deleted_items += 1
                except Exception as e:
                    failed_deletions += 1
                    print(f"Failed to delete image: {str(e)}")

            # Handle videos
            if "video" in message and message["video"]:
                try:
                    video_path = message["video"].split("/o/")[1].split("?")[0]
                    video_path = urllib.parse.unquote(video_path)

                    blob = storage.bucket().blob(video_path)
                    if blob.exists():
                        blob.delete()
                        deleted_items += 1
                except Exception as e:
                    failed_deletions += 1
                    print(f"Failed to delete video: {str(e)}")

    # Remove room from all users
    users_collection.update_many(
        {"rooms": room_code},
        {"$pull": {"rooms": room_code}, "$set": {"current_room": None}},
    )

    # Delete the room
    rooms_collection.delete_one({"_id": room_code})

    if deleted_items > 0 or failed_deletions > 0:
        flash(
            f"Room deleted. Successfully removed {deleted_items} media files. {failed_deletions} files failed to delete."
        )
    else:
        flash("Room successfully deleted.")

    return redirect(url_for("home"))


def handle_room_operation(username, code, create, join):
    room = code
    if create:
        room = generate_unique_code()
        room_name = request.form.get(
            "room_name", "Unnamed Room"
        )  # Get custom name from form
        rooms_collection.insert_one(
            {
                "_id": room,
                "name": room_name,  # Add custom name
                "users": [username],
                "messages": [],
                "created_by": username,
            }
        )
    elif join:
        room_exists = rooms_collection.find_one({"_id": code})
        if not room_exists:
            flash("Room does not exist.")
            return redirect(url_for("home"))

        # Add user to the room's user list only if they're not already in it
        rooms_collection.update_one({"_id": code}, {"$addToSet": {"users": username}})

    session["room"] = room
    session["name"] = username

    # Update user's current room and rooms list
    users_collection.update_one(
        {"username": username},
        {"$set": {"current_room": room}, "$addToSet": {"rooms": room}},
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
            room_users.append(
                {
                    "username": user_name,
                    "online": user_data.get("online", False),
                    "current_room": user_data.get("current_room"),
                }
            )

    return render_template(
        "room_settings.html",
        room_code=room_code,
        room_data=room_data,
        room_users=room_users,
        current_user=current_user,
        is_owner=(room_data["created_by"] == username),
    )


@app.route("/search_messages/<room_code>")
@login_required
def search_messages(room_code):
    username = current_user.username
    query = request.args.get("q", "").strip().lower()

    if not query:
        return jsonify({"messages": []})

    room_data = rooms_collection.find_one({"_id": room_code})

    if not room_data or username not in room_data["users"]:
        return jsonify({"error": "Access denied"}), 403

    # Search messages
    matching_messages = [
        msg for msg in room_data["messages"] if query in msg.get("message", "").lower()
    ]

    return jsonify({"messages": matching_messages[-50:]})  # Return last 50 matches


@app.route("/kick_user/<room_code>/<username>", methods=["POST"])
@login_required
def kick_user(room_code, username):
    current_username = current_user.username
    room_data = rooms_collection.find_one({"_id": room_code})

    if not room_data:
        return jsonify({"success": False, "message": "Room not found"}), 404

    if room_data["created_by"] != current_username:
        return (
            jsonify({"success": False, "message": "Only room owner can kick users"}),
            403,
        )

    if username == current_username:
        return (
            jsonify({"success": False, "message": "You cannot kick yourself"}),
            400,
        )

    if username == room_data["created_by"]:
        return (
            jsonify({"success": False, "message": "Cannot kick room owner"}),
            400,
        )

    if username not in room_data["users"]:
        return jsonify({"success": False, "message": "User not in room"}), 404

    # Remove user from room
    rooms_collection.update_one({"_id": room_code}, {"$pull": {"users": username}})

    # Update kicked user's data
    users_collection.update_one(
        {"username": username},
        {"$pull": {"rooms": room_code}, "$set": {"current_room": None}},
    )

    # Emit socket event to notify kicked user
    socketio.emit("user_kicked", {"room": room_code}, room=username)

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


def get_message_type(message):
    """Helper function to determine message type"""
    if "video" in message and message["video"]:
        return "video"
    elif "image" in message and message["image"]:
        return "image"
    elif "file" in message and message["file"]:
        return "file"
    elif "message" in message and message["message"]:
        return "text"
    return "unknown"


def get_message_content(message):
    """Helper function to get appropriate message content based on type"""
    message_type = get_message_type(message)

    if message_type == "video":
        return "📹 Video"
    elif message_type == "image":
        return "📷 Image"
    elif message_type == "file":
        return "📎 File"
    elif message_type == "text":
        return message.get("message", "")
    return "Unknown message type"


@app.route("/get_last_message/<room_code>")
@login_required
def get_last_message(room_code):
    try:
        room_data = rooms_collection.find_one({"_id": room_code})
        if not room_data or not room_data.get("messages"):
            return jsonify({"last_message": None})

        last_message = room_data["messages"][-1]

        message_content = get_message_content(last_message)

        return jsonify(
            {
                "last_message": {
                    "content": message_content,
                    "sender": last_message.get("name", ""),
                    "timestamp": last_message.get("timestamp", ""),
                    "type": get_message_type(last_message),
                }
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# In your room route, update the rooms_with_messages preparation:
def prepare_room_message_data(room_info):
    last_message = room_info["messages"][-1] if room_info.get("messages") else None
    if last_message:
        message_content = get_message_content(last_message)
        message_type = get_message_type(last_message)
    else:
        message_content = ""
        message_type = "none"

    return {
        "content": message_content,
        "sender": last_message["name"] if last_message else "",
        "timestamp": last_message.get("timestamp", "") if last_message else "",
        "type": message_type,
    }


@app.route("/room/", defaults={"code": None}, methods=["GET", "POST"])
@app.route("/room/<code>", methods=["GET", "POST"])
@login_required
def room(code):
    username = current_user.username

    # Handle POST requests (room creation/joining)
    if request.method == "POST":
        if request.form.get("create"):
            room_name = request.form.get("room_name")
            if not room_name:
                flash("Room name is required")
                return redirect(url_for("room", code=code))

            # Generate a unique room code
            new_room_code = generate_unique_code()

            # Create new room document
            new_room = {
                "_id": new_room_code,
                "name": room_name,
                "users": [username],
                "messages": [],
                "created_by": username,
                "created_at": datetime.utcnow(),
            }

            try:
                rooms_collection.insert_one(new_room)

                users_collection.update_one(
                    {"username": username},
                    {
                        "$addToSet": {"rooms": new_room_code},
                        "$set": {"current_room": new_room_code},
                    },
                )

                session["room"] = new_room_code
                return redirect(url_for("room", code=new_room_code))

            except Exception as e:
                flash(f"Error creating room: {str(e)}")
                return redirect(url_for("room", code=code))

        elif request.form.get("join"):
            join_code = request.form.get("code")
            if not join_code:
                flash("Room code is required")
                return redirect(url_for("room", code=code))

            join_room = rooms_collection.find_one({"_id": join_code})
            if not join_room:
                flash("Room does not exist")
                return redirect(url_for("room", code=code))

            try:
                rooms_collection.update_one(
                    {"_id": join_code}, {"$addToSet": {"users": username}}
                )

                users_collection.update_one(
                    {"username": username},
                    {
                        "$addToSet": {"rooms": join_code},
                        "$set": {"current_room": join_code},
                    },
                )

                session["room"] = join_code
                return redirect(url_for("room", code=join_code))

            except Exception as e:
                flash(f"Error joining room: {str(e)}")
                return redirect(url_for("room", code=code))

    room_data = rooms_collection.find_one({"_id": code})
    if not room_data:
        flash("Room does not exist")
        return redirect(url_for("home"))

    session["room"] = code
    session["name"] = username

    try:
        user_data = users_collection.find_one({"username": username})
        if not user_data:
            flash("User data not found")
            return redirect(url_for("home"))

        users_collection.update_one(
            {"username": username}, {"$set": {"current_room": code}}
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
                user_list.append(
                    {
                        "username": user,
                        "online": user_profile.get("online", False),
                        "isFriend": user in user_friends,
                    }
                )

        unread_messages = get_unread_messages(username)

        friends_data = []
        for friend in user_friends:
            friend_data = users_collection.find_one({"username": friend})
            if friend_data:
                room_name = "Unknown Room"
                if friend_data.get("current_room"):
                    friend_room_data = get_room_data(friend_data["current_room"])
                    if friend_room_data:
                        room_name = friend_room_data.get("name", "Unnamed Room")

                friends_data.append(
                    {
                        "username": friend,
                        "online": friend_data.get("online", False),
                        "current_room": friend_data.get("current_room"),
                        "room_name": room_name,
                    }
                )

        # Prepare room data with last messages and sort by timestamp
        rooms_with_messages = []
        for room_code in user_data.get("rooms", []):
            room_info = get_room_data(room_code)
            if room_info:
                last_message_data = prepare_room_message_data(room_info)
                rooms_with_messages.append(
                    {
                        "code": room_code,
                        "name": room_info.get("name", "Unnamed Room"),
                        "profile_photo": room_info.get("profile_photo"),
                        "users": room_info.get("users", []),
                        "last_message": last_message_data,
                        "unread_count": unread_messages.get(str(room_code), {}).get(
                            "unread_count", 0
                        ),
                    }
                )

        # Sort rooms by last_message timestamp (most recent first)
        rooms_with_messages.sort(
            key=lambda room: room["last_message"].get("timestamp", datetime.min),
            reverse=True,
        )

        return render_template(
            "room.html",
            code=code,
            room_name=room_data["name"],
            messages=room_data["messages"],
            users=user_list,
            username=username,
            created_by=room_data["created_by"],
            friends=friends_data,
            room_data=room_data,
            user_data=user_data,
            rooms_with_messages=rooms_with_messages,
        )

    except Exception as e:
        flash(f"Error loading room data: {str(e)}")
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
        flash(
            "Room owners cannot leave their own rooms. You must delete the room instead."
        )
        return redirect(url_for("home"))

    # Update user data
    result = users_collection.update_one(
        {"username": username},
        {"$pull": {"rooms": code}, "$set": {"current_room": None}},
    )

    # Always remove the user from the room's user list when exiting
    rooms_collection.update_one({"_id": code}, {"$pull": {"users": username}})

    # Add system message about user leaving
    system_message = {
        "id": str(ObjectId()),
        "name": "system",
        "message": f"{username} has left the room",
        "type": "system",
        "read_by": room_data["users"],  # Mark as read by all current users
    }
    rooms_collection.update_one({"_id": code}, {"$push": {"messages": system_message}})

    # Emit the system message to all users in the room
    socketio.emit("message", system_message, to=code)

    flash("You have left the room successfully.")
    return redirect(url_for("home"))


@app.route("/update_room_name/<room_code>", methods=["POST"])
@login_required
def update_room_name(room_code):
    username = current_user.username
    new_name = request.form.get("room_name", "").strip()

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
    rooms_collection.update_one({"_id": room_code}, {"$set": {"name": new_name}})

    flash("Room name updated successfully.")
    return redirect(url_for("room", code=room_code))


@app.route("/update_room_photo/<room_code>", methods=["POST"])
@login_required
def update_room_photo(room_code):
    # Check if user is room owner
    room_data = rooms_collection.find_one({"_id": room_code})

    if not room_data:
        return jsonify({"error": "Room not found"}), 404

    if "photo" not in request.files:
        return jsonify({"error": "No photo provided"}), 400

    photo = request.files["photo"]
    if photo.filename == "":
        return jsonify({"error": "No photo selected"}), 400

    if not allowed_file(photo.filename):
        return jsonify({"error": "Invalid file type"}), 400

    try:
        # Delete existing room photo if it exists
        existing_photo_url = room_data.get("profile_photo")
        if existing_photo_url:
            try:
                delete_firebase_image(existing_photo_url)
            except Exception as e:
                print(f"Error deleting existing room photo: {e}")

        # Open the image using Pillow
        img = Image.open(photo)

        # Convert the image to webp
        img = img.convert("RGB")  # Ensure it's in RGB mode for webp
        img_byte_arr = io.BytesIO()  # Create a byte stream to save the image in memory
        img.save(img_byte_arr, format="WEBP")  # Save as webp in the byte array
        img_byte_arr.seek(0)  # Move to the beginning of the byte array

        # Generate unique filename using room code
        filename = f"room_profile_photos/{room_code}.webp"

        # Upload to Firebase Storage
        bucket = storage.bucket()
        blob = bucket.blob(filename)
        blob.content_type = "image/webp"  # Set content type to webp

        # Upload the file from the byte array
        blob.upload_from_file(img_byte_arr, content_type="image/webp")

        # Make the blob publicly accessible
        blob.make_public()

        # Get the public URL
        photo_url = blob.public_url

        # Update room document with new photo URL
        rooms_collection.update_one(
            {"_id": room_code}, {"$set": {"profile_photo": photo_url}}
        )

        return jsonify({"photo_url": photo_url}), 200

    except Exception as e:
        print(f"Error uploading room photo: {e}")
        return jsonify({"error": "Failed to upload photo"}), 500


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
        {"_id": room, "messages.id": message_id}, {"messages.$": 1}
    )

    if not message or not message.get("messages"):
        return

    current_message = message["messages"][0]
    current_reactions = current_message.get("reactions", {})
    current_emoji_data = current_reactions.get(emoji, {"count": 0, "users": []})

    if username in current_emoji_data.get("users", []):
        # Remove user's reaction
        update_result = rooms_collection.update_one(
            {"_id": room, "messages.id": message_id},
            {
                "$pull": {f"messages.$[msg].reactions.{emoji}.users": username},
                "$inc": {f"messages.$[msg].reactions.{emoji}.count": -1},
            },
            array_filters=[{"msg.id": message_id}],
        )

        # If the reaction count is now 0, remove the empty reaction in a separate operation
        if current_emoji_data["count"] == 1:  # This was the last reaction
            rooms_collection.update_one(
                {"_id": room, "messages.id": message_id},
                {"$unset": {f"messages.$[msg].reactions.{emoji}": ""}},
                array_filters=[{"msg.id": message_id}],
            )
    else:
        # Add user's reaction
        rooms_collection.update_one(
            {"_id": room, "messages.id": message_id},
            {
                "$set": {
                    f"messages.$[msg].reactions.{emoji}": {
                        "count": current_emoji_data.get("count", 0) + 1,
                        "users": current_emoji_data.get("users", []) + [username],
                    }
                }
            },
            array_filters=[{"msg.id": message_id}],
        )

    try:
        # Emit updated reactions to all users in the room
        updated_message = rooms_collection.find_one(
            {"_id": room, "messages.id": message_id}, {"messages.$": 1}
        )

        if updated_message and updated_message.get("messages"):
            emit(
                "update_reactions",
                {
                    "messageId": message_id,
                    "reactions": updated_message["messages"][0].get("reactions", {}),
                },
                room=room,
            )
    except Exception as e:
        print(f"Error emitting reaction update: {str(e)}")


@app.route("/upload_video", methods=["POST"])
@login_required
def upload_video():
    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    video = request.files["video"]
    if not is_valid_video(video):
        return (
            jsonify({"error": "Invalid video file or size exceeds 50MB"}),
            400,
        )

    try:
        # Save original video temporarily
        with tempfile.NamedTemporaryFile(delete=False) as temp_input:
            video.save(temp_input.name)

        # Compress and convert to WEBM
        output_path = compress_convert_video(temp_input.name)

        # Upload to Firebase Storage
        filename = secure_filename(f"{str(ObjectId())}.webm")
        bucket = storage.bucket()
        blob = bucket.blob(f"room_videos/{filename}")

        # Upload the processed video
        blob.upload_from_filename(output_path)

        # Clean up temporary files
        os.unlink(temp_input.name)
        os.unlink(output_path)

        # Generate public URL
        video_url = blob.generate_signed_url(timedelta(days=7))

        return jsonify({"url": video_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def compress_convert_video(input_file):
    """Compress video and convert to WEBM format"""
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as temp_output:
        output_path = temp_output.name

    try:
        # Convert and compress video using ffmpeg
        stream = ffmpeg.input(input_file)
        stream = ffmpeg.output(
            stream,
            output_path,
            **{
                "c:v": "libvpx-vp9",  # VP9 codec for WEBM
                "crf": VIDEO_COMPRESS_CRF,  # Compression quality
                "b:v": "1M",  # Target bitrate
                "maxrate": "1.5M",  # Maximum bitrate
                "bufsize": "2M",  # Buffer size
                "c:a": "libopus",  # Audio codec
                "b:a": "128k",  # Audio bitrate
            },
        )
        ffmpeg.run(
            stream,
            overwrite_output=True,
            capture_stdout=True,
            capture_stderr=True,
        )

        return output_path
    except ffmpeg.Error as e:
        print(f"FFmpeg error: {e.stderr.decode()}")
        if os.path.exists(output_path):
            os.unlink(output_path)
        raise


def is_valid_video(file):
    """Check if file is a valid video and within size limits"""
    if not file:
        return False

    # Check file size (50MB limit)
    file_size_mb = len(file.read()) / (1024 * 1024)
    file.seek(0)  # Reset file pointer
    if file_size_mb > MAX_VIDEO_SIZE_MB:
        return False

    # Check MIME type using file extension
    file_type, _ = mimetypes.guess_type(file.name)
    return file_type in ALLOWED_VIDEO_TYPES


@socketio.on("find_message")
def find_message(data):
    room = session.get("room")
    message_id = data.get("message_id")

    if not room or not message_id:
        socketio.emit("message_found", {"found": False}, room=request.sid)
        return

    # Find message in room
    message_data = rooms_collection.find_one(
        {"_id": room, "messages.id": message_id}, {"messages.$": 1}
    )

    if not message_data:
        socketio.emit("message_found", {"found": False}, room=request.sid)
        return

    # Get message index
    room_data = rooms_collection.find_one({"_id": room})
    message_index = next(
        (i for i, msg in enumerate(room_data["messages"]) if msg["id"] == message_id),
        None,
    )

    if message_index is None:
        socketio.emit("message_found", {"found": False}, room=request.sid)
        return

    # Get messages around target (10 before, 10 after)
    start_index = max(0, message_index - 10)
    end_index = min(len(room_data["messages"]), message_index + 11)
    context_messages = room_data["messages"][start_index:end_index]

    # Format messages
    messages_with_read_status = []
    for msg in context_messages:
        msg_copy = msg.copy()
        msg_copy["read_by"] = msg_copy.get("read_by", [])
        for key, value in msg_copy.items():
            if isinstance(value, datetime):
                msg_copy[key] = datetime_to_iso(value)
        messages_with_read_status.append(msg_copy)

    socketio.emit(
        "message_found",
        {
            "found": True,
            "messages": messages_with_read_status,
            "has_more": start_index > 0 or end_index < len(room_data["messages"]),
        },
        room=request.sid,
    )


@app.route("/update_timezone", methods=["POST"])
@login_required
def update_timezone():
    timezone = request.json.get("timezone")
    if timezone in pytz.all_timezones:
        users_collection.update_one(
            {"username": current_user.username}, {"$set": {"timezone": timezone}}
        )
        session["timezone"] = timezone
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Invalid timezone"}), 400

TENOR_API_KEY = os.getenv("TENOR_API_KEY")
TENOR_BASE_URL = "https://tenor.googleapis.com/v2"

def validate_gif_data(gif: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Validates and sanitizes GIF data before saving to database.
    Returns None if invalid, sanitized dict if valid.
    """
    if not gif or not isinstance(gif, dict):
        return None
        
    required_fields = {'url', 'title'}
    if not all(field in gif for field in required_fields):
        return None
        
    return {
        'url': str(gif['url']),
        'title': str(gif['title']),
        'saved_at': datetime.now(timezone.utc).isoformat()
    }

@app.route("/api/search-gifs")
def search_gifs():
    query = request.args.get("q", "")
    limit = int(request.args.get("limit", 20))
    
    try:
        response = requests.get(
            f"{TENOR_BASE_URL}/search",
            params={
                "q": query or "trending",
                "key": TENOR_API_KEY,
                "client_key": "web",
                "limit": limit,
                "media_filter": "minimal"
            }
        )
        response.raise_for_status()
        return jsonify(response.json())
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500

@socketio.on("message")
def message(data):
    room = session.get("room")
    room_data = rooms_collection.find_one({"_id": room})
    if not room or not room_data:
        return
    
    # Handle GIF data with validation
    gif_data = validate_gif_data(data.get("gif"))

    # Handle reply_to data structure
    reply_to = None
    if data.get("replyTo"):
        if isinstance(data["replyTo"], dict):
            reply_to = {
                "id": data["replyTo"]["id"],
                "message": data["replyTo"]["message"],
            }
        else:
            original_message = rooms_collection.find_one(
                {"_id": room, "messages.id": data["replyTo"]}, 
                {"messages.$": 1}
            )
            if original_message and original_message.get("messages"):
                reply_to = {
                    "id": data["replyTo"],
                    "message": original_message["messages"][0]["message"],
                }

    content = {
        "id": str(ObjectId()),
        "name": current_user.username,
        "message": data["data"],
        "reply_to": reply_to,
        "read_by": [session.get("username")],
        "image": data.get("image"),
        "video": data.get("video"),
        "gif": gif_data,  # Using validated gif data
        "reactions": {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Create the message update operation
    update_operation = {"$push": {"messages": content}}
    
    # Add metadata for GIF messages
    if gif_data:
        update_operation["$inc"] = {"gif_count": 1}
        update_operation["$set"] = {
            "last_gif_sent": datetime.now(timezone.utc).isoformat()
        }

    # Update the room with the new message
    rooms_collection.update_one({"_id": room}, update_operation)

    # Send to room
    send(content, to=room)

    # Schedule notifications
    sender_username = current_user.username
    room_users = room_data["users"]

    for username in room_users:
        if username != sender_username:
            socketio.start_background_task(
                check_and_notify,
                message_id=content["id"],
                room_id=room,
                recipient_username=username,
                sender_username=sender_username,
                message_text=content["message"],
            )

def check_and_notify(
    message_id, room_id, recipient_username, sender_username, message_text
):
    # Wait for 5 seconds
    socketio.sleep(5)

    # Check if the message is still unread
    room = rooms_collection.find_one({"_id": room_id})
    if not room:
        return

    message = next((msg for msg in room["messages"] if msg["id"] == message_id), None)
    if not message:
        return

    # If the recipient hasn't read the message after 5 seconds, send a notification
    if recipient_username not in message.get("read_by", []):
        send_notification(
            recipient_username=recipient_username,
            sender_username=sender_username,
            message_text=message_text,
        )


@socketio.on("mark_messages_read")
def mark_messages_read(data):
    room = session.get("room")
    username = current_user.username
    if not room or not username:
        return

    # Update the read status of messages in the room
    rooms_collection.update_many(
        {
            "_id": room,
            "messages": {
                "$elemMatch": {
                    "id": {"$in": data["message_ids"]},
                    "read_by": {"$ne": username},
                }
            },
        },
        {
            "$addToSet": {"messages.$[elem].read_by": username},
        },
        array_filters=[{"elem.id": {"$in": data["message_ids"]}}],
    )

    # Emit an event to notify other users that messages have been read
    socketio.emit(
        "messages_read",
        {
            "reader": username,
            "message_ids": data["message_ids"],
        },
        room=room,
    )


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
            if (
                username not in message.get("read_by", [])
                and message["name"] != username
            ):
                unread_count += 1

                # Determine message content based on type
                if "image" in message:
                    content = "📷 Image"
                elif "file" in message:
                    content = "📎 File"
                elif "message" in message:
                    content = message["message"]
                else:
                    content = "Unknown message type"

                unread_msg_details.append(
                    {
                        "id": message["id"],
                        "sender": message["name"],
                        "content": content,
                    }
                )

        if unread_count > 0:
            unread_messages[room_id] = {
                "unread_count": unread_count,
                "messages": unread_msg_details,
            }

    return unread_messages


@app.route("/get_unread_messages")
@login_required
def fetch_unread_messages():
    username = current_user.username
    if not username:
        return jsonify({"error": "User not logged in"}), 401

    unread_messages = get_unread_messages(username)
    return jsonify(unread_messages)


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

    last_message_index = next(
        (i for i, msg in enumerate(all_messages) if msg["id"] == last_message_id),
        None,
    )

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

    socketio.emit(
        "more_messages",
        {"messages": messages_with_read_status, "has_more": start_index > 0},
        room=request.sid,
    )


@socketio.on("connect")
def connect():
    room = session.get("room")
    username = current_user.username
    if not room or not username:
        return

    join_room(room)

    # Get user and room data
    room_data = rooms_collection.find_one({"_id": room})
    user_data = users_collection.find_one({"username": username})

    if not room_data or not user_data:
        return

    # Check if this is the user's first time joining the room
    # Make sure to check both the rooms array and handle the case where it doesn't exist
    user_rooms = user_data.get("rooms", [])
    is_first_join = room not in user_rooms

    # Update user's current room and rooms list
    users_collection.update_one(
        {"username": username},
        {"$set": {"current_room": room}, "$addToSet": {"rooms": room}},
    )

    # Add user to the room's user list if not already present
    rooms_collection.update_one({"_id": room}, {"$addToSet": {"users": username}})

    # If this is the user's first time joining, add a system message
    if is_first_join:
        current_time = datetime.utcnow()
        system_message = {
            "id": str(ObjectId()),
            "name": "system",
            "message": f"{username} has joined the room for the first time",
            "type": "system",
            "read_by": room_data.get("users", []),  # Mark as read by all current users
        }

        # Add the system message to the room
        rooms_collection.update_one(
            {"_id": room}, {"$push": {"messages": system_message}}
        )

        # Emit the system message to all users in the room
        socketio.emit(
            "message",
            {
                **system_message,
            },
            to=room,
        )

    # Get updated room data for user list
    room_data = rooms_collection.find_one({"_id": room})
    user_data = users_collection.find_one({"username": username})

    # Send updated user list with online status and friend information
    user_list = []
    for user in room_data.get("users", []):
        user_profile = users_collection.find_one({"username": user})
        if user_profile:
            user_list.append(
                {
                    "username": user,
                    "online": user_profile.get("online", False),
                    "isFriend": user in user_data.get("friends", []),
                }
            )

    socketio.emit(
        "update_users",
        {
            "users": user_list,
            "room_name": room_data.get("name", "Unnamed Room"),
        },
        room=room,
    )

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

    socketio.emit(
        "chat_history",
        {
            "messages": messages_with_read_status,
            "has_more": len(messages) < len(room_data.get("messages", [])),
            "room_name": room_data.get("name", "Unnamed Room"),
        },
        room=request.sid,
    )


@socketio.on("disconnect")
def disconnect():
    username = current_user.username
    room = session.get("room")

    if not username or not room:
        return

    leave_room(room)

    # Update user profile
    users_collection.update_one(
        {"username": username}, {"$set": {"current_room": None}}
    )

    # Note: We no longer remove the user from the room's user list here

    # Get updated room data and notify remaining users
    room_data = rooms_collection.find_one({"_id": room})
    user_list = []
    for user in room_data["users"]:
        user_profile = users_collection.find_one({"username": user})
        user_list.append(
            {
                "username": user,
                "online": user_profile.get("online", False),
                "isFriend": False,
            }
        )
    socketio.emit("update_users", {"users": user_list}, room=room)


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
                    "name": name,  # Ensure only message owner can edit
                }
            },
        },
        {
            "$set": {
                "messages.$[elem].message": data["newText"],
                "messages.$[elem].edited": True,
            }
        },
        array_filters=[{"elem.id": data["messageId"]}],
    )

    if result.modified_count:
        socketio.emit(
            "edit_message",
            {"messageId": data["messageId"], "newText": data["newText"]},
            room=room,
        )


@socketio.on("delete_message")
def delete_message(data):
    room = session.get("room")
    name = session.get("name")
    if not room:
        return

    # Remove message from MongoDB
    result = rooms_collection.update_one(
        {"_id": room},
        {"$pull": {"messages": {"id": data["messageId"], "name": name}}},
    )

    if result.modified_count:
        socketio.emit("delete_message", {"messageId": data["messageId"]}, room=room)


@socketio.on("typing")
def handle_typing(data):
    room = session.get("room")
    if room:
        name = session.get("name")
        socketio.emit(
            "typing",
            {"name": name, "isTyping": data.get("isTyping", False)},
            room=room,
            include_self=False,
        )


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
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True, host="0.0.0.0", port=port)
