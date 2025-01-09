# Standard library imports
import os
import random
import re
import io
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
import urllib.parse
import string
import tempfile
import mimetypes

# Third-party library imports
import requests
import pytz
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
from fastapi import FastAPI, Request, Response, Depends, HTTPException, File, UploadFile, Form
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import jwt
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_401_UNAUTHORIZED
import uvicorn

# Load environment variables
load_dotenv()

# Initialize Firebase
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred, {"storageBucket": "channelchat-7d679.appspot.com"})

# Initialize FastAPI
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY"))

# Initialize templates
templates = Jinja2Templates(directory="templates")

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize scheduler
scheduler = BackgroundScheduler()

# MongoDB setup
client = MongoClient(os.getenv("MONGO_URI"))
db = client["chat_app_db"]

# Collections
users_collection = db["users"]
rooms_collection = db["rooms"]
heartbeats_collection = db["heartbeats"]

# Create indexes
users_collection.create_index([("username", 1)], unique=True)
users_collection.create_index([("friends", 1)])
users_collection.create_index([("current_room", 1)])
rooms_collection.create_index([("users", 1)])
rooms_collection.create_index([("messages.id", 1)])
users_collection.create_index([("fcm_token", 1)])

# Constants
ALLOWED_IMAGE_TYPES = {"png", "jpeg", "jpg", "gif"}
MAX_VIDEO_SIZE_MB = 50
ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
}
VIDEO_COMPRESS_CRF = 28

# JWT settings
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

# OAuth2 scheme
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Pydantic models
class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None

class UserBase(BaseModel):
    username: str

class UserCreate(UserBase):
    password: str

class User(UserBase):
    friends: List[str] = []
    friend_requests: List[str] = []
    current_room: Optional[str] = None
    online: bool = False
    rooms: List[str] = []
    profile_photo: Optional[str] = None

class NotificationSettings(BaseModel):
    enabled: bool = False
    
class PasswordUpdate(BaseModel):
    current_password: str
    new_password: str
    confirm_new_password: str

class FriendRequest(BaseModel):
    friend_username: str

class UserSuggestion(BaseModel):
    username: str
    profile_photo_url: str
    similarity: int

class UserInvite(BaseModel):
    room: str
    room_name: str
    from_user: str
    profile_photo: Optional[str]

class PendingInvite(BaseModel):
    username: str
    room: str
    room_name: str
    profile_photo: Optional[str]
    
class RoomMessage(BaseModel):
    message: str
    name: str
    time: datetime
    image: Optional[str] = None
    video: Optional[str] = None

class RoomUser(BaseModel):
    username: str
    online: bool
    current_room: Optional[str]

class RoomData(BaseModel):
    id: str
    name: str
    users: List[str]
    messages: List[RoomMessage]
    created_by: str
    profile_photo: Optional[str] = None
    
class MessageContent(BaseModel):
    content: str
    sender: str
    timestamp: str
    type: str

class LastMessage(BaseModel):
    content: str
    sender: str
    timestamp: datetime
    type: str

class RoomInfo(BaseModel):
    code: str
    name: str
    profile_photo: Optional[str]
    users: List[str]
    last_message: LastMessage
    unread_count: int

class UserInfo(BaseModel):
    username: str
    online: bool
    isFriend: bool

class FriendInfo(BaseModel):
    username: str
    online: bool
    current_room: Optional[str]
    room_name: str

# Helper functions
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except jwt.JWTError:
        raise credentials_exception
    user = get_user_data(token_data.username)
    if user is None:
        raise credentials_exception
    return user

def get_user_data(username: str):
    user_data = users_collection.find_one({"username": username})
    if user_data:
        user_data["_id"] = str(user_data["_id"])
        if "room_invites" not in user_data:
            user_data["room_invites"] = []
            users_collection.update_one(
                {"username": username}, {"$set": {"room_invites": []}}
            )
    return user_data

def update_user_data(username, data):
    """Update user data in MongoDB"""
    if "_id" in data:
        del data["_id"]  # Remove _id if present to avoid update errors
    users_collection.update_one({"username": username}, {"$set": data})
    
def generate_unique_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if not rooms_collection.find_one({"_id": code}):
            return code

def allowed_file(filename: str):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_TYPES

# Routes
@app.post("/token")
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user_data = users_collection.find_one({"username": form_data.username})
    if not user_data or not check_password_hash(user_data["password"], form_data.password):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data={"sub": form_data.username})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/register")
async def register(user: UserCreate):
    if not re.match(r"^[a-zA-Z0-9_.-]+$", user.username):
        raise HTTPException(status_code=400, detail="Invalid username format")
    
    if len(user.password) < 8:
        raise HTTPException(status_code=400, detail="Password too short")
    
    if not re.search(r"[a-zA-Z]", user.password) or not re.search(r"\d", user.password):
        raise HTTPException(status_code=400, detail="Password must contain letters and numbers")

    if users_collection.find_one({"username": user.username}):
        raise HTTPException(status_code=400, detail="Username already exists")

    user_data = {
        "username": user.username,
        "password": generate_password_hash(user.password),
        "friends": [],
        "friend_requests": [],
        "current_room": None,
        "online": False,
        "rooms": [],
    }

    try:
        users_collection.insert_one(user_data)
        return {"message": "Registration successful"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Registration failed")
    
    # Additional routes and dependencies
@app.post("/logout")
async def logout(current_user: User = Depends(get_current_user)):
    username = current_user["username"]
    
    # Remove heartbeat entry
    heartbeats_collection.delete_one({"username": username})
    
    # Update user's online status
    users_collection.update_one(
        {"username": username},
        {"$set": {"online": False}}
    )
    
    return {"message": "Logged out successfully"}

@app.post("/delete_account")
async def delete_account(current_user: User = Depends(get_current_user)):
    username = current_user["username"]
    deleted_images = 0
    failed_deletions = 0
    
    # Get user data to check for profile photo
    user_data = users_collection.find_one({"username": username})
    if user_data and user_data.get("profile_photo"):
        try:
            profile_photo_url = user_data["profile_photo"]
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
    
    for room in rooms_to_delete:
        room_code = room["_id"]
        
        # Delete room profile photo
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
                        print(f"Failed to delete image for message: {message.get('id', 'unknown')}")
                        print(f"Error: {str(e)}")
        
        # Remove room from all users
        users_collection.update_many(
            {"rooms": room_code},
            {"$pull": {"rooms": room_code}, "$set": {"current_room": None}}
        )
    
    # Delete all rooms created by the user
    rooms_collection.delete_many({"created_by": username})
    
    # Remove user from all rooms
    rooms_collection.update_many(
        {"members": username},
        {"$pull": {"members": username}}
    )
    
    # Remove friend relationships
    users_collection.update_many(
        {"friends": username},
        {"$pull": {"friends": username}}
    )
    
    # Remove pending friend requests
    users_collection.update_many(
        {"friend_requests": username},
        {"$pull": {"friend_requests": username}}
    )
    
    # Delete heartbeat entry
    heartbeats_collection.delete_one({"username": username})
    
    # Delete user
    users_collection.delete_one({"username": username})
    
    return {
        "message": "Account deleted successfully",
        "deleted_images": deleted_images,
        "failed_deletions": failed_deletions
    }

@app.post("/settings/password")
async def update_password(
    password_update: PasswordUpdate,
    current_user: User = Depends(get_current_user)
):
    user_data = users_collection.find_one({"username": current_user["username"]})
    
    if not check_password_hash(user_data["password"], password_update.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    
    if len(password_update.new_password) < 8 or not any(
        char.isdigit() for char in password_update.new_password
    ) or not any(char.isalpha() for char in password_update.new_password):
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters long and include letters and numbers"
        )
    
    if password_update.new_password != password_update.confirm_new_password:
        raise HTTPException(status_code=400, detail="New passwords do not match")
    
    users_collection.update_one(
        {"username": current_user["username"]},
        {"$set": {"password": generate_password_hash(password_update.new_password)}}
    )
    
    return {"message": "Password updated successfully"}

@app.post("/add_friend")
async def add_friend(
    friend_request: FriendRequest,
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    friend_username = friend_request.friend_username
    
    if not friend_username:
        raise HTTPException(status_code=400, detail="Please enter a username")
    
    if friend_username == username:
        raise HTTPException(status_code=400, detail="You cannot add yourself as a friend")
    
    friend_data = users_collection.find_one({"username": friend_username})
    if not friend_data:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_data = users_collection.find_one({"username": username})
    
    if friend_username in user_data.get("friends", []):
        raise HTTPException(status_code=400, detail="Already friends")
    
    if friend_username in user_data.get("friend_requests", []):
        raise HTTPException(
            status_code=400,
            detail="This user has already sent you a friend request"
        )
    
    users_collection.update_one(
        {"username": friend_username},
        {"$addToSet": {"friend_requests": username}}
    )
    
    return {"message": f"Friend request sent to {friend_username}"}

@app.post("/accept_friend/{username}")
async def accept_friend(
    username: str,
    current_user: User = Depends(get_current_user)
):
    current_username = current_user["username"]
    
    result = users_collection.update_one(
        {"username": current_username, "friend_requests": username},
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
        return {"message": f"You are now friends with {username}"}
    
    raise HTTPException(status_code=404, detail="No friend request found")

@app.post("/decline_friend/{username}")
async def decline_friend(
    username: str,
    current_user: User = Depends(get_current_user)
):
    result = users_collection.update_one(
        {"username": current_user["username"]},
        {"$pull": {"friend_requests": username}}
    )
    
    if result.modified_count:
        return {"message": f"Friend request from {username} declined"}
    
    raise HTTPException(status_code=404, detail="No friend request found")

@app.post("/remove_friend/{username}")
async def remove_friend(
    username: str,
    current_user: User = Depends(get_current_user)
):
    current_username = current_user["username"]
    
    result = users_collection.update_one(
        {"username": current_username, "friends": username},
        {"$pull": {"friends": username}}
    )
    
    if result.modified_count:
        users_collection.update_one(
            {"username": username},
            {"$pull": {"friends": current_username}}
        )
        return {"message": "Friend removed successfully"}
    
    raise HTTPException(status_code=400, detail="Not friends")

@app.get("/")
async def home(current_user: User = Depends(get_current_user)):
    username = current_user["username"]
    
    user_data = users_collection.find_one({"username": username})
    if not user_data:
        user_data = {
            "username": username,
            "rooms": [],
            "friends": [],
            "friend_requests": [],
            "online": True,
            "current_room": None
        }
        users_collection.insert_one(user_data)
    
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
    
    return {
        "username": username,
        "user_data": user_data,
        "friends": friends_data,
        "friend_requests": user_data.get("friend_requests", [])
    }

@app.post("/notification-settings")
async def update_notification_settings(
    settings: NotificationSettings,
    current_user: User = Depends(get_current_user)
):
    try:
        users_collection.update_one(
            {"username": current_user["username"]},
            {"$set": {"notification_settings": settings.dict()}}
        )
        return {"message": "Settings updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/register-fcm-token")
async def register_fcm_token(
    token: str,
    clear_all: bool = False,
    current_user: User = Depends(get_current_user)
):
    try:
        if clear_all:
            users_collection.update_one(
                {"username": current_user["username"]},
                {
                    "$unset": {
                        "fcm_token": "",
                        "notification_settings.enabled": "",
                    }
                },
            )
            return {"message": "Notification settings cleared"}

        if not token:
            raise HTTPException(status_code=400, detail="Token is required")

        users_collection.update_one(
            {"username": current_user["username"]},
            {
                "$set": {
                    "fcm_token": token,
                    "notification_settings.enabled": True,
                }
            },
        )
        return {"message": "FCM token registered successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
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


@app.post("/update_profile_photo/{username}")
async def update_profile_photo(
    username: str,
    photo: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    if current_user["username"] != username:
        raise HTTPException(status_code=403, detail="Not authorized")

    if not allowed_file(photo.filename):
        raise HTTPException(status_code=400, detail="Invalid file type")

    try:
        # Process and upload image similar to Flask version
        image = Image.open(io.BytesIO(await photo.read()))
        
        # Verify file format
        if image.format.lower() not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid image type. Allowed types: {', '.join(ALLOWED_IMAGE_TYPES).upper()}"
            )

        # Check file size
        await photo.seek(0)
        content = await photo.read()
        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB")

        # Get existing photo URL
        user_data = users_collection.find_one({"username": username})
        if not user_data:
            raise HTTPException(status_code=404, detail="User not found")

        existing_photo_url = user_data.get("profile_photo")
        if existing_photo_url:
            try:
                delete_firebase_image(existing_photo_url)
            except Exception as e:
                print(f"Error deleting existing profile photo: {e}")

        # Process and upload image
        image.thumbnail((200, 200))
        img_io = io.BytesIO()
        image.save(img_io, format=image.format)
        img_io.seek(0)

        filename = f"profile_photos/{username}.{image.format.lower()}"
        bucket = storage.bucket()
        blob = bucket.blob(filename)
        blob.content_type = f"image/{image.format.lower()}"
        blob.upload_from_file(img_io, content_type=blob.content_type)
        blob.make_public()
        photo_url = blob.public_url

        users_collection.update_one(
            {"username": username},
            {"$set": {"profile_photo": photo_url}}
        )

        return {"photo_url": photo_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload photo: {str(e)}")

# Scheduler functions
def check_inactive_users():
    threshold = datetime.utcnow() - timedelta(minutes=5)
    inactive_users = heartbeats_collection.find({"last_heartbeat": {"$lt": threshold}})

    for user in inactive_users:
        users_collection.update_one(
            {"username": user["username"]},
            {"$set": {"online": False}}
        )
        heartbeats_collection.delete_one({"_id": user["_id"]})

# Start scheduler
@app.on_event("startup")
async def startup_event():
    if not scheduler.running:
        scheduler.add_job(func=check_inactive_users, trigger="interval", minutes=1)
        scheduler.start()

@app.on_event("shutdown")
async def shutdown_event():
    if scheduler.running:
        scheduler.shutdown()

# Heartbeat routes
@app.post("/heartbeat")
async def heartbeat(current_user: User = Depends(get_current_user)):
    heartbeats_collection.update_one(
        {"username": current_user["username"]},
        {"$set": {"last_heartbeat": datetime.utcnow()}},
        upsert=True
    )
    users_collection.update_one(
        {"username": current_user["username"]},
        {"$set": {"online": True}}
    )
    return Response(status_code=204)

@app.post("/stop_heartbeat")
async def stop_heartbeat(current_user: User = Depends(get_current_user)):
    heartbeats_collection.delete_one({"username": current_user["username"]})
    users_collection.update_one(
        {"username": current_user["username"]},
        {"$set": {"online": False}}
    )
    return Response(status_code=204)

@app.get("/search_users", response_model=List[UserSuggestion])
async def search_users(
    q: str,
    current_user: User = Depends(get_current_user)
):
    query = q.lower().strip()
    if not query:
        return []

    # Get current user's data for exclusion list
    current_user_data = users_collection.find_one({"username": current_user["username"]})
    friends_list = current_user_data.get("friends", [])

    # First, get all eligible users
    all_users = list(
        users_collection.find(
            {
                "username": {
                    "$ne": current_user["username"],
                    "$nin": friends_list
                }
            },
            {"username": 1}
        )
    )

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

    # Format response
    return [
        UserSuggestion(
            username=user["username"],
            profile_photo_url=f"/profile_photo/{user['username']}",
            similarity=fuzz.ratio(query, user["username"].lower())
        )
        for user in matching_users
    ]

@app.get("/pending_room_invites")
async def pending_room_invites(
    current_user: User = Depends(get_current_user)
):
    # Get current user's data
    user_data = get_user_data(current_user["username"])
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    # Initialize pending_invites if it doesn't exist
    if "pending_invites" not in user_data:
        user_data["pending_invites"] = []

    return {"user_data": user_data}

@app.get("/cancel_room_invite/{username}/{room_code}")
async def cancel_room_invite(
    username: str,
    room_code: str,
    current_user: User = Depends(get_current_user)
):
    # Get the invited user's data
    friend_data = get_user_data(username)
    if not friend_data:
        raise HTTPException(status_code=404, detail="User not found")

    # Remove the invite from their room_invites
    if "room_invites" in friend_data:
        friend_data["room_invites"] = [
            inv for inv in friend_data["room_invites"]
            if inv.get("room") != room_code
        ]
        update_user_data(username, friend_data)

    # Remove from current user's pending_invites
    user_data = get_user_data(current_user["username"])
    if "pending_invites" in user_data:
        user_data["pending_invites"] = [
            inv for inv in user_data["pending_invites"]
            if inv.get("username") != username or inv.get("room") != room_code
        ]
        update_user_data(current_user["username"], user_data)

    return {"message": f"Cancelled room invitation to {username}"}

@app.get("/invite_to_room/{username}")
async def invite_to_room(
    username: str,
    current_user: User = Depends(get_current_user),
    current_room: Optional[str] = None
):
    if not current_room:
        raise HTTPException(status_code=400, detail="You're not in a room")

    # Get the room data
    room_data = rooms_collection.find_one({"_id": current_room})
    if not room_data:
        raise HTTPException(status_code=404, detail="Room not found")

    # Get the friend's data
    friend_data = get_user_data(username)
    if not friend_data:
        raise HTTPException(status_code=404, detail="User not found")

    # Get current user's data
    user_data = get_user_data(current_user["username"])

    if username not in user_data.get("friends", []):
        raise HTTPException(status_code=403, detail="You can only invite friends to rooms")

    # Initialize room_invites for friend
    if "room_invites" not in friend_data:
        friend_data["room_invites"] = []

    # Initialize pending_invites for current user
    if "pending_invites" not in user_data:
        user_data["pending_invites"] = []

    # Check if invite already exists
    existing_invite = next(
        (inv for inv in friend_data["room_invites"] if inv.get("room") == current_room),
        None
    )

    if not existing_invite:
        # Create new invite
        new_invite = UserInvite(
            room=current_room,
            room_name=room_data.get("name", "Unnamed Room"),
            from_user=current_user["username"],
            profile_photo=room_data.get("profile_photo")
        )
        friend_data["room_invites"].append(new_invite.dict())

        # Add to pending_invites for current user
        pending_invite = PendingInvite(
            username=username,
            room=current_room,
            room_name=room_data.get("name", "Unnamed Room"),
            profile_photo=room_data.get("profile_photo")
        )
        user_data["pending_invites"].append(pending_invite.dict())

        # Save both updated data
        update_user_data(username, friend_data)
        update_user_data(current_user["username"], user_data)
        return {"message": f"Room invitation sent to {username}!"}
    
    return {"message": f"{username} already has a pending invite to this room"}

@app.post("/accept_room_invite/{room_code}")
async def accept_room_invite(
    room_code: str,
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    user_data = get_user_data(username)

    # Find the invite
    room_invites = user_data.get("room_invites", [])
    invite = next((inv for inv in room_invites if inv["room"] == room_code), None)

    if not invite:
        raise HTTPException(
            status_code=404,
            detail="Room invite not found or already accepted"
        )

    sender_username = invite["from"]

    # Filter out the accepted invite
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
            inv for inv in sender_data["pending_invites"]
            if not (inv["username"] == username and inv["room"] == room_code)
        ]
        update_user_data(sender_username, sender_data)

    return {"message": "Room invite accepted", "room_code": room_code}

@app.post("/decline_room_invite/{room_code}")
async def decline_room_invite(
    room_code: str,
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    user_data = get_user_data(username)

    room_invites = user_data.get("room_invites", [])
    invite = next((inv for inv in room_invites if inv["room"] == room_code), None)

    if not invite:
        raise HTTPException(
            status_code=404,
            detail="Room invite not found or already declined"
        )

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
            inv for inv in sender_data["pending_invites"]
            if not (inv["username"] == username and inv["room"] == room_code)
        ]
        update_user_data(sender_username, sender_data)

    return {"message": "Room invite declined"}

@app.post("/join_friend_room/{friend_username}")
async def join_friend_room(
    friend_username: str,
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    user_data = users_collection.find_one({"username": username})

    if friend_username not in user_data.get("friends", []):
        raise HTTPException(
            status_code=403,
            detail="User is not in your friends list"
        )

    friend_data = users_collection.find_one({"username": friend_username})
    friend_room = friend_data.get("current_room")

    if not friend_room:
        raise HTTPException(
            status_code=404,
            detail="Friend is not in any room"
        )

    room_exists = rooms_collection.find_one({"_id": friend_room})
    if not room_exists:
        raise HTTPException(
            status_code=404,
            detail="Friend's room no longer exists"
        )

    # Update user's current room
    users_collection.update_one(
        {"username": username},
        {"$set": {"current_room": friend_room}}
    )

    return {"message": "Joined friend's room", "room_code": friend_room}

@app.delete("/delete_room/{room_code}")
async def delete_room(
    room_code: str,
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    room_data = rooms_collection.find_one({"_id": room_code})

    if not room_data:
        raise HTTPException(status_code=404, detail="Room does not exist")

    if room_data["created_by"] != username:
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to delete this room"
        )

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

    # Delete message media
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
        {"$pull": {"rooms": room_code}, "$set": {"current_room": None}}
    )

    # Delete the room
    rooms_collection.delete_one({"_id": room_code})

    return {
        "message": f"Room deleted. Successfully removed {deleted_items} media files. {failed_deletions} files failed to delete."
    }

async def handle_room_operation(
    username: str,
    code: Optional[str],
    create: bool,
    join: bool,
    room_name: Optional[str] = None
):
    room = code
    if create:
        room = generate_unique_code()
        rooms_collection.insert_one({
            "_id": room,
            "name": room_name or "Unnamed Room",
            "users": [username],
            "messages": [],
            "created_by": username
        })
    elif join:
        room_exists = rooms_collection.find_one({"_id": code})
        if not room_exists:
            raise HTTPException(status_code=404, detail="Room does not exist")

        # Add user to the room's user list
        rooms_collection.update_one(
            {"_id": code},
            {"$addToSet": {"users": username}}
        )

    # Update user's current room and rooms list
    users_collection.update_one(
        {"username": username},
        {
            "$set": {"current_room": room},
            "$addToSet": {"rooms": room}
        }
    )

    return {"room_code": room}


@app.get("/room_settings/{room_code}")
async def room_settings(
    room_code: str,
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    room_data = rooms_collection.find_one({"_id": room_code})

    if not room_data:
        raise HTTPException(status_code=404, detail="Room does not exist")

    if username not in room_data["users"]:
        raise HTTPException(status_code=403, detail="You don't have access to this room")

    # Get detailed user information for each room member
    room_users = []
    for user_name in room_data["users"]:
        user_data = users_collection.find_one({"username": user_name})
        if user_data:
            room_users.append(RoomUser(
                username=user_name,
                online=user_data.get("online", False),
                current_room=user_data.get("current_room")
            ))

    return {
        "room_code": room_code,
        "room_data": room_data,
        "room_users": room_users,
        "is_owner": room_data["created_by"] == username
    }
    
@app.get("/search_messages/{room_code}")
async def search_messages(
    room_code: str,
    q: str,
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    query = q.strip().lower()

    if not query:
        return {"messages": []}

    room_data = rooms_collection.find_one({"_id": room_code})

    if not room_data or username not in room_data["users"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # Search messages
    matching_messages = [
        msg for msg in room_data["messages"]
        if query in msg.get("message", "").lower()
    ]

    return {"messages": matching_messages[-50:]}  # Return last 50 matches

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
        self.user_rooms: Dict[str, Set[str]] = {}

    async def connect(self, websocket: WebSocket, room_id: str, user_id: str):
        await websocket.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = {}
        self.active_connections[room_id][user_id] = websocket
        
        if user_id not in self.user_rooms:
            self.user_rooms[user_id] = set()
        self.user_rooms[user_id].add(room_id)

    def disconnect(self, room_id: str, user_id: str):
        if room_id in self.active_connections:
            self.active_connections[room_id].pop(user_id, None)
            if not self.active_connections[room_id]:
                self.active_connections.pop(room_id)
        
        if user_id in self.user_rooms:
            self.user_rooms[user_id].remove(room_id)
            if not self.user_rooms[user_id]:
                self.user_rooms.pop(user_id)

    async def broadcast_to_room(self, room_id: str, message: dict, exclude_user: Optional[str] = None):
        if room_id in self.active_connections:
            for user_id, connection in self.active_connections[room_id].items():
                if user_id != exclude_user:
                    await connection.send_json(message)

    async def send_personal_message(self, user_id: str, message: dict):
        for room_id in self.user_rooms.get(user_id, set()):
            if room_id in self.active_connections and user_id in self.active_connections[room_id]:
                await self.active_connections[room_id][user_id].send_json(message)

manager = ConnectionManager()

# Helper functions
def get_message_type(message: dict) -> str:
    if message.get("video"):
        return "video"
    elif message.get("image"):
        return "image"
    elif message.get("gif"):
        return "gif"
    elif message.get("file"):
        return "file"
    elif message.get("message"):
        return "text"
    return "unknown"

def get_message_content(message: dict) -> str:
    message_type = get_message_type(message)
    type_contents = {
        "video": "📽 Video",
        "image": "📷 Image",
        "gif": "🎥 GIF",
        "file": "📎 File",
        "text": message.get("message", ""),
        "unknown": "Unknown message type"
    }
    return type_contents[message_type]

def get_room_data(room_code: str) -> Optional[dict]:
    try:
        room_data = rooms_collection.find_one({"_id": room_code})
        if not room_data:
            return None

        room_data.setdefault("users", [])
        room_data.setdefault("messages", [])
        room_data.setdefault("created_by", "Unknown")
        return room_data
    except Exception:
        return None

def prepare_room_message_data(room_info: dict) -> dict:
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

# WebSocket endpoints
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, current_user: User = Depends(get_current_user)):
    await manager.connect(websocket, room_id, current_user["username"])
    try:
        while True:
            data = await websocket.receive_json()
            
            # Handle different message types
            if data["type"] == "message":
                message = {
                    "type": "message",
                    "content": data["content"],
                    "sender": current_user["username"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                
                # Save message to database
                rooms_collection.update_one(
                    {"_id": room_id},
                    {"$push": {"messages": message}}
                )
                
                # Broadcast to all users in room
                await manager.broadcast_to_room(room_id, message)
                
            elif data["type"] == "user_typing":
                typing_notification = {
                    "type": "user_typing",
                    "username": current_user["username"]
                }
                await manager.broadcast_to_room(room_id, typing_notification, exclude_user=current_user["username"])

    except WebSocketDisconnect:
        manager.disconnect(room_id, current_user["username"])
        await manager.broadcast_to_room(
            room_id,
            {
                "type": "user_disconnected",
                "username": current_user["username"]
            }
        )

# HTTP endpoints
@app.get("/get_last_message/{room_code}", response_model=Optional[MessageContent])
async def get_last_message(
    room_code: str,
    current_user: User = Depends(get_current_user)
):
    try:
        room_data = rooms_collection.find_one({"_id": room_code})
        if not room_data or not room_data.get("messages"):
            return None

        last_message = room_data["messages"][-1]
        message_content = get_message_content(last_message)

        return MessageContent(
            content=message_content,
            sender=last_message.get("name", ""),
            timestamp=last_message.get("timestamp", ""),
            type=get_message_type(last_message)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/room")
@app.get("/room")
@app.get("/room/{code}")
async def room(
    code: Optional[str] = None,
    create: bool = False,
    join: bool = False,
    room_name: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]

    # Handle room creation
    if create:
        if not room_name:
            raise HTTPException(status_code=400, detail="Room name is required")

        new_room_code = generate_unique_code()
        new_room = {
            "_id": new_room_code,
            "name": room_name,
            "users": [username],
            "messages": [],
            "created_by": username,
            "created_at": datetime.utcnow()
        }

        try:
            rooms_collection.insert_one(new_room)
            users_collection.update_one(
                {"username": username},
                {
                    "$addToSet": {"rooms": new_room_code},
                    "$set": {"current_room": new_room_code}
                }
            )
            return {"room_code": new_room_code}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error creating room: {str(e)}")

    # Handle room joining
    if join and code:
        join_room = rooms_collection.find_one({"_id": code})
        if not join_room:
            raise HTTPException(status_code=404, detail="Room does not exist")

        try:
            rooms_collection.update_one(
                {"_id": code},
                {"$addToSet": {"users": username}}
            )
            users_collection.update_one(
                {"username": username},
                {
                    "$addToSet": {"rooms": code},
                    "$set": {"current_room": code}
                }
            )
            return {"room_code": code}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error joining room: {str(e)}")

    # Get room data
    if code:
        room_data = get_room_data(code)
        if not room_data:
            raise HTTPException(status_code=404, detail="Room does not exist")

        try:
            user_data = users_collection.find_one({"username": username})
            if not user_data:
                raise HTTPException(status_code=404, detail="User data not found")

            users_collection.update_one(
                {"username": username},
                {"$set": {"current_room": code}}
            )

            user_friends = set(user_data.get("friends", []))

            # Process messages
            for message in room_data["messages"]:
                message["is_friend"] = message["name"] in user_friends

            # Get user list
            user_list = []
            for user in room_data["users"]:
                user_profile = users_collection.find_one({"username": user})
                if user_profile:
                    user_list.append(UserInfo(
                        username=user,
                        online=user_profile.get("online", False),
                        isFriend=user in user_friends
                    ))

            # Get friends data
            friends_data = []
            for friend in user_friends:
                friend_data = users_collection.find_one({"username": friend})
                if friend_data:
                    room_name = "Unknown Room"
                    if friend_data.get("current_room"):
                        friend_room_data = get_room_data(friend_data["current_room"])
                        if friend_room_data:
                            room_name = friend_room_data.get("name", "Unnamed Room")

                    friends_data.append(FriendInfo(
                        username=friend,
                        online=friend_data.get("online", False),
                        current_room=friend_data.get("current_room"),
                        room_name=room_name
                    ))

            # Get rooms with messages
            unread_messages = get_unread_messages(username)
            rooms_with_messages = []
            for room_code in user_data.get("rooms", []):
                room_info = get_room_data(room_code)
                if room_info:
                    last_message_data = prepare_room_message_data(room_info)
                    rooms_with_messages.append(RoomInfo(
                        code=room_code,
                        name=room_info.get("name", "Unnamed Room"),
                        profile_photo=room_info.get("profile_photo"),
                        users=room_info.get("users", []),
                        last_message=last_message_data,
                        unread_count=unread_messages.get(str(room_code), {}).get("unread_count", 0)
                    ))

            # Sort rooms by last message timestamp
            rooms_with_messages.sort(
                key=lambda room: room.last_message.timestamp,
                reverse=True
            )

            return {
                "code": code,
                "room_name": room_data["name"],
                "messages": room_data["messages"],
                "users": user_list,
                "username": username,
                "created_by": room_data["created_by"],
                "friends": friends_data,
                "room_data": room_data,
                "user_data": user_data,
                "rooms_with_messages": rooms_with_messages
            }

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error loading room data: {str(e)}")

    return {"message": "No room code provided"}

@app.delete("/room/{room_code}/exit")
async def exit_room(
    room_code: str,
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    user_data = users_collection.find_one({"username": username})

    # Verify the room exists
    room_data = rooms_collection.find_one({"_id": room_code})
    if not room_data:
        raise HTTPException(status_code=404, detail="Room does not exist")

    # Verify user is not the room owner
    if room_data["created_by"] == username:
        raise HTTPException(
            status_code=400,
            detail="Room owners cannot leave their own rooms. You must delete the room instead."
        )

    try:
        # Update user data
        users_collection.update_one(
            {"username": username},
            {"$pull": {"rooms": room_code}, "$set": {"current_room": None}}
        )

        # Remove user from room's user list
        rooms_collection.update_one(
            {"_id": room_code},
            {"$pull": {"users": username}}
        )

        # Add system message about user leaving
        system_message = {
            "id": str(ObjectId()),
            "name": "system",
            "message": f"{username} has left the room",
            "type": "system",
            "read_by": room_data["users"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        rooms_collection.update_one(
            {"_id": room_code},
            {"$push": {"messages": system_message}}
        )

        # Broadcast the system message through WebSocket
        await manager.broadcast_to_room(room_code, system_message)

        return {"message": "Successfully left the room"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error exiting room: {str(e)}")


@app.put("/room/{room_code}/name")
async def update_room_name(
    room_code: str,
    room_name: str = Form(...),
    current_user: User = Depends(get_current_user)
):
    username = current_user["username"]

    if not room_name.strip():
        raise HTTPException(status_code=400, detail="Room name cannot be empty")

    room_data = rooms_collection.find_one({"_id": room_code})
    if not room_data:
        raise HTTPException(status_code=404, detail="Room does not exist")

    if room_data["created_by"] != username:
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to update this room's name"
        )

    try:
        rooms_collection.update_one(
            {"_id": room_code},
            {"$set": {"name": room_name.strip()}}
        )
        return {"message": "Room name updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating room name: {str(e)}")


@app.put("/room/{room_code}/photo")
async def update_room_photo(
    room_code: str,
    photo: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    # Check if room exists
    room_data = rooms_collection.find_one({"_id": room_code})
    if not room_data:
        raise HTTPException(status_code=404, detail="Room not found")

    # Validate file type
    if not allowed_file(photo.filename):
        raise HTTPException(status_code=400, detail="Invalid file type")

    try:
        # Delete existing room photo if it exists
        existing_photo_url = room_data.get("profile_photo")
        if existing_photo_url:
            try:
                await delete_firebase_image(existing_photo_url)
            except Exception as e:
                print(f"Error deleting existing room photo: {e}")

        # Read the uploaded file
        contents = await photo.read()
        
        # Open the image using Pillow
        img = Image.open(io.BytesIO(contents))

        # Convert the image to webp
        img = img.convert("RGB")
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format="WEBP")
        img_byte_arr.seek(0)

        # Generate unique filename
        filename = f"room_profile_photos/{room_code}.webp"

        # Upload to Firebase Storage
        bucket = storage.bucket()
        blob = bucket.blob(filename)
        blob.content_type = "image/webp"

        # Upload the file from byte array
        blob.upload_from_file(img_byte_arr, content_type="image/webp")
        blob.make_public()

        # Get the public URL
        photo_url = blob.public_url

        # Update room document
        rooms_collection.update_one(
            {"_id": room_code},
            {"$set": {"profile_photo": photo_url}}
        )

        return {"photo_url": photo_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error uploading photo: {str(e)}")

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

@app.route("/api/gif-categories")
def gif_categories():
    try:
        response = requests.get(
            f"{TENOR_BASE_URL}/categories",
            params={
                "key": TENOR_API_KEY,
                "client_key": "web",
                "type": "featured"  # Get featured categories
            }
        )
        response.raise_for_status()
        return jsonify(response.json())
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/search-suggestions")
def search_suggestions():
    try:
        response = requests.get(
            f"{TENOR_BASE_URL}/trending_terms",
            params={
                "key": TENOR_API_KEY,
                "client_key": "web",
                "limit": 8  # Limit to top 8 trending terms
            }
        )
        response.raise_for_status()
        return jsonify(response.json())
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/autocomplete-gifs")
def autocomplete_gifs():
    query = request.args.get("q", "")
    
    if not query:
        return jsonify({"results": []})
        
    try:
        response = requests.get(
            f"{TENOR_BASE_URL}/autocomplete",
            params={
                "q": query,
                "key": TENOR_API_KEY,
                "client_key": "web",
                "limit": 5  # Limit to top 5 suggestions
            }
        )
        response.raise_for_status()
        return jsonify(response.json())
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/search-gifs")
def search_gifs():
    query = request.args.get("q", "")
    limit = int(request.args.get("limit", 16))
    
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
                elif "gif" in message:
                    content = "🎥 GIF"
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
