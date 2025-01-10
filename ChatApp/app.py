# Standard library imports
import io
import mimetypes
import os
import random
import re
import string
import tempfile
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

# Third-party library imports
import ffmpeg
from jose import JWTError, jwt
import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, storage
from fastapi.staticfiles import StaticFiles
from firebase_admin import initialize_app
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from fuzzywuzzy import fuzz
from PIL import Image
from pymongo import MongoClient
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from passlib.context import CryptContext
from werkzeug.utils import secure_filename
import uvicorn
from fastapi_login import LoginManager
from fastapi_login.exceptions import InvalidCredentialsException

# Load environment variables
load_dotenv()

# Initialize Firebase
if not firebase_admin._apps:
    cred = credentials.Certificate("serviceAccountKey.json")
    initialize_app(cred, {"storageBucket": "channelchat-7d679.appspot.com"})

# Security configurations
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Initialize FastAPI-Login
manager = LoginManager(SECRET_KEY, '/login', use_cookie=True)
manager.cookie_name = "chat_access_token"

# Password context for hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Initialize FastAPI
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# Initialize templates
templates = Jinja2Templates(directory="templates")

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize scheduler
scheduler = BackgroundScheduler()

# MongoDB setup
client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
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

# Pydantic models
class MessageContent(BaseModel):
    content: str
    sender: str
    timestamp: str
    type: str

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None

class UserBase(BaseModel):
    username: str
    full_name: Optional[str] = None

class UserCreate(UserBase):
    password: str

class UserInfo(BaseModel):
    username: str
    online: bool
    isFriend: bool

class UserSuggestion(BaseModel):
    username: str
    profile_photo_url: str
    similarity: int

class User(UserBase):
    disabled: Optional[bool] = False
    friends: List[str] = []
    friend_requests: List[str] = []
    current_room: Optional[str] = None
    online: bool = False
    rooms: List[str] = []
    profile_photo: Optional[str] = None

class FriendRequest(BaseModel):
    friend_username: str

class UserInvite(BaseModel):
    room: str
    room_name: str
    from_user: str
    profile_photo: Optional[str] = None

class PendingInvite(BaseModel):
    username: str
    room: str
    room_name: str
    profile_photo: Optional[str] = None

class UserInDB(User):
    hashed_password: str

class NotificationSettings(BaseModel):
    enabled: bool = False

class PasswordUpdate(BaseModel):
    current_password: str
    new_password: str
    confirm_new_password: str

class LoadMoreMessages(BaseModel):
    last_message_id: str

class MessageRead(BaseModel):
    message_ids: List[str]

class Message(BaseModel):
    id: str
    name: str
    message: str
    type: str
    read_by: List[str]
    edited: Optional[bool] = False
    timestamp: datetime

class RoomBase(BaseModel):
    name: str
    created_by: str

class RoomCreate(RoomBase):
    users: List[str]

class RoomUser(BaseModel):
    username: str
    online: bool
    current_room: Optional[str] = None

class RoomInfo(BaseModel):
    code: str
    name: str
    profile_photo: Optional[str] = None
    users: List[str]
    last_message: MessageContent
    unread_count: int

class Room(RoomBase):
    id: str
    users: List[str]
    messages: List[Message] = []
    profile_photo: Optional[str] = None

# Update the user loader for FastAPI-Login
@manager.user_loader()
async def load_user(username: str):
    user = await users_collection.find_one({"username": username})
    if user:
        return UserInDB(**user)
    return None

# Dependency to get the current user
async def get_current_user(current_user: User = Depends(manager)):
    return current_user

# Helper functions
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def generate_unique_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if not rooms_collection.find_one({"_id": code}):
            return code

def allowed_file(filename: str):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_TYPES

# Authentication routes
@app.route("/login", methods=["GET", "POST"])
async def login(
    request: Request,
    username: str = Form(None),
    password: str = Form(None),
    remember_me: bool = Form(False)
):
    context = {"request": request, "message": None}
    
    # Handle GET request (display form)
    if request.method == "GET":
        return templates.TemplateResponse("login.html", context)
    
    # Handle POST request (form submission)
    try:
        form_data = OAuth2PasswordRequestForm(
            username=username,
            password=password,
            scope=""
        )
        
        user = await load_user(username)
        if not user:
            context["message"] = "Invalid username or password"
            return templates.TemplateResponse("login.html", context)
        
        if not verify_password(password, user.hashed_password):
            context["message"] = "Invalid username or password"
            return templates.TemplateResponse("login.html", context)
        
        # Update user's online status
        await users_collection.update_one(
            {"username": username},
            {"$set": {"online": True}}
        )
        
        # Create access token
        access_token = manager.create_access_token(
            data={"sub": username},
            expires=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        
        # Create response with redirect
        response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        manager.set_cookie(response, access_token)
        
        # Set remember me cookie if selected
        if remember_me:
            response.set_cookie(
                "remember_token",
                access_token,
                max_age=30 * 24 * 60 * 60,  # 30 days
                httponly=True
            )
            
        return response
        
    except Exception as e:
        context["message"] = "An error occurred during login"
        return templates.TemplateResponse("login.html", context)

@app.route("/register", methods=["GET", "POST"])
async def register(request: Request):
    context = {"request": request, "message": None}

    # Handle GET request (display form)
    if request.method == "GET":
        return templates.TemplateResponse("register.html", context)

    # Handle POST request (form submission)
    try:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")
        confirm_password = form.get("confirm_password")

        # Validate form inputs
        if not username or not password or not confirm_password:
            context["message"] = "All fields are required."
            return templates.TemplateResponse("register.html", context)

        if password != confirm_password:
            context["message"] = "Passwords do not match."
            return templates.TemplateResponse("register.html", context)

        # Validate username format
        if not re.match(r"^[a-zA-Z0-9_.-]+$", username):
            context["message"] = "Invalid username format."
            return templates.TemplateResponse("register.html", context)

        # Validate password strength
        if len(password) < 8:
            context["message"] = "Password must be at least 8 characters long."
            return templates.TemplateResponse("register.html", context)

        if not re.search(r"[a-zA-Z]", password) or not re.search(r"\d", password):
            context["message"] = "Password must contain both letters and numbers."
            return templates.TemplateResponse("register.html", context)

        # Check if username already exists
        existing_user = await users_collection.find_one({"username": username})
        if existing_user:
            context["message"] = "Username already registered."
            return templates.TemplateResponse("register.html", context)

        # Create and store the new user
        hashed_password = get_password_hash(password)
        user_data = {
            "username": username,
            "hashed_password": hashed_password,
            "friends": [],
            "friend_requests": [],
            "current_room": None,
            "online": False,
            "rooms": [],
            "disabled": False,
        }

        await users_collection.insert_one(user_data)

        # Redirect to login page after successful registration
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    except Exception as e:
        context["message"] = "An error occurred during registration."
        return templates.TemplateResponse("register.html", context)

@app.post("/logout")
async def logout(user: User = Depends(manager)):
    # Update user's online status and remove from current room
    await users_collection.update_one(
        {"username": user.username},
        {"$set": {"online": False, "current_room": None}}
    )

    # Remove heartbeat entry
    await heartbeats_collection.delete_one({"username": user.username})

    response = JSONResponse({"message": "Logged out successfully"})
    manager.set_cookie(response, "")  # Clear the cookie
    return response

# Friend management routes
@app.post("/friend-request")
async def send_friend_request(request: FriendRequest, current_user: User = Depends(manager)):
    friend = await users_collection.find_one({"username": request.friend_username})
    if not friend:
        raise HTTPException(status_code=404, detail="User not found")
    
    if request.friend_username == current_user.username:
        raise HTTPException(status_code=400, detail="Cannot send friend request to yourself")
    
    if request.friend_username in current_user.friends:
        raise HTTPException(status_code=400, detail="Already friends with this user")
    
    await users_collection.update_one(
        {"username": request.friend_username},
        {"$addToSet": {"friend_requests": current_user.username}}
    )
    
    return {"message": "Friend request sent successfully"}

@app.post("/accept-friend")
async def accept_friend_request(request: FriendRequest, current_user: User = Depends(manager)):
    if request.friend_username not in current_user.friend_requests:
        raise HTTPException(status_code=400, detail="No friend request from this user")
    
    # Add each user to the other's friends list and remove the friend request
    await users_collection.update_one(
        {"username": current_user.username},
        {
            "$addToSet": {"friends": request.friend_username},
            "$pull": {"friend_requests": request.friend_username}
        }
    )
    
    await users_collection.update_one(
        {"username": request.friend_username},
        {"$addToSet": {"friends": current_user.username}}
    )
    
    return {"message": "Friend request accepted"}

@app.post("/reject-friend")
async def reject_friend_request(request: FriendRequest, current_user: User = Depends(manager)):
    if request.friend_username not in current_user.friend_requests:
        raise HTTPException(status_code=400, detail="No friend request from this user")
    
    await users_collection.update_one(
        {"username": current_user.username},
        {"$pull": {"friend_requests": request.friend_username}}
    )
    
    return {"message": "Friend request rejected"}

@app.post("/remove-friend")
async def remove_friend(request: FriendRequest, current_user: User = Depends(manager)):
    if request.friend_username not in current_user.friends:
        raise HTTPException(status_code=400, detail="Not friends with this user")
    
    # Remove each user from the other's friends list
    await users_collection.update_one(
        {"username": current_user.username},
        {"$pull": {"friends": request.friend_username}}
    )
    
    await users_collection.update_one(
        {"username": request.friend_username},
        {"$pull": {"friends": current_user.username}}
    )
    
    return {"message": "Friend removed successfully"}

# Room management routes
@app.post("/create-room", response_model=Room)
async def create_room(room: RoomCreate, current_user: User = Depends(manager)):
    room_id = generate_unique_code()
    room_data = room.dict()
    room_data["id"] = room_id
    
    # Ensure all users exist
    for username in room.users:
        user = await users_collection.find_one({"username": username})
        if not user:
            raise HTTPException(status_code=404, detail=f"User {username} not found")
    
    # Create the room
    new_room = Room(**room_data)
    await rooms_collection.insert_one(new_room.dict())
    
    # Add room to users' room lists
    for username in room.users:
        await users_collection.update_one(
            {"username": username},
            {"$addToSet": {"rooms": room_id}}
        )
    
    return new_room

@app.post("/join-room/{room_id}")
async def join_room(room_id: str, current_user: User = Depends(manager)):
    room = await rooms_collection.find_one({"id": room_id})
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    
    if current_user.username in room["users"]:
        raise HTTPException(status_code=400, detail="Already in this room")
    
    # Add user to room and room to user's list
    await rooms_collection.update_one(
        {"id": room_id},
        {"$addToSet": {"users": current_user.username}}
    )
    
    await users_collection.update_one(
        {"username": current_user.username},
        {"$addToSet": {"rooms": room_id}}
    )
    
    return {"message": "Joined room successfully"}

@app.post("/leave-room/{room_id}")
async def leave_room(room_id: str, current_user: User = Depends(manager)):
    room = await rooms_collection.find_one({"id": room_id})
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    
    if current_user.username not in room["users"]:
        raise HTTPException(status_code=400, detail="Not in this room")
    
    # Remove user from room and room from user's list
    await rooms_collection.update_one(
        {"id": room_id},
        {"$pull": {"users": current_user.username}}
    )
    
    await users_collection.update_one(
        {"username": current_user.username},
        {
            "$pull": {"rooms": room_id},
            "$set": {"current_room": None} if current_user.current_room == room_id else {}
        }
    )
    
    # If room is empty, delete it
    updated_room = await rooms_collection.find_one({"id": room_id})
    if not updated_room["users"]:
        await rooms_collection.delete_one({"id": room_id})
        return {"message": "Left room and room was deleted"}
    
    return {"message": "Left room successfully"}

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

    for room_to_delete in rooms_to_delete:
        room_code = room_to_delete["_id"]

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
        if "messages" in room_to_delete:
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

        # Remove room from all users
        users_collection.update_many(
            {"rooms": room_code},
            {"$pull": {"rooms": room_code}, "$set": {"current_room": None}},
        )

    # Delete all rooms created by the user
    rooms_collection.delete_many({"created_by": username})

    # Remove user from all rooms
    rooms_collection.update_many(
        {"members": username}, {"$pull": {"members": username}}
    )

    # Remove friend relationships
    users_collection.update_many(
        {"friends": username}, {"$pull": {"friends": username}}
    )

    # Remove pending friend requests
    users_collection.update_many(
        {"friend_requests": username}, {"$pull": {"friend_requests": username}}
    )

    # Delete heartbeat entry
    heartbeats_collection.delete_one({"username": username})

    # Delete user
    users_collection.delete_one({"username": username})

    return {
        "message": "Account deleted successfully",
        "deleted_images": deleted_images,
        "failed_deletions": failed_deletions,
    }


@app.post("/settings/password")
async def update_password(
    password_update: PasswordUpdate, current_user: User = Depends(get_current_user)
):
    user_data = users_collection.find_one({"username": current_user["username"]})

    if not verify_password(
        password_update.current_password, user_data["hashed_password"]
    ):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if (
        len(password_update.new_password) < 8
        or not any(char.isdigit() for char in password_update.new_password)
        or not any(char.isalpha() for char in password_update.new_password)
    ):
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters long and include letters and numbers",
        )

    if password_update.new_password != password_update.confirm_new_password:
        raise HTTPException(status_code=400, detail="New passwords do not match")

    users_collection.update_one(
        {"username": current_user["username"]},
        {"$set": {"password": get_password_hash(password_update.new_password)}},
    )

    return {"message": "Password updated successfully"}


@app.post("/add_friend")
async def add_friend(
    friend_request: FriendRequest, current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    friend_username = friend_request.friend_username

    if not friend_username:
        raise HTTPException(status_code=400, detail="Please enter a username")

    if friend_username == username:
        raise HTTPException(
            status_code=400, detail="You cannot add yourself as a friend"
        )

    friend_data = users_collection.find_one({"username": friend_username})
    if not friend_data:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = users_collection.find_one({"username": username})

    if friend_username in user_data.get("friends", []):
        raise HTTPException(status_code=400, detail="Already friends")

    if friend_username in user_data.get("friend_requests", []):
        raise HTTPException(
            status_code=400, detail="This user has already sent you a friend request"
        )

    users_collection.update_one(
        {"username": friend_username}, {"$addToSet": {"friend_requests": username}}
    )

    return {"message": f"Friend request sent to {friend_username}"}


@app.post("/accept_friend/{username}")
async def accept_friend(username: str, current_user: User = Depends(get_current_user)):
    current_username = current_user["username"]

    result = users_collection.update_one(
        {"username": current_username, "friend_requests": username},
        {"$pull": {"friend_requests": username}, "$addToSet": {"friends": username}},
    )

    if result.modified_count:
        users_collection.update_one(
            {"username": username}, {"$addToSet": {"friends": current_username}}
        )
        return {"message": f"You are now friends with {username}"}

    raise HTTPException(status_code=404, detail="No friend request found")


@app.post("/decline_friend/{username}")
async def decline_friend(username: str, current_user: User = Depends(get_current_user)):
    result = users_collection.update_one(
        {"username": current_user["username"]}, {"$pull": {"friend_requests": username}}
    )

    if result.modified_count:
        return {"message": f"Friend request from {username} declined"}

    raise HTTPException(status_code=404, detail="No friend request found")

@app.route("/", methods=["GET"])
async def home(request: Request, current_user: User = Depends(get_current_user)):
    context = {"request": request, "message": None}

    try:
        # Get current user's username
        username = current_user["username"]

        # Fetch user data or initialize it if not found
        user_data = await users_collection.find_one({"username": username})
        if not user_data:
            user_data = {
                "username": username,
                "rooms": [],
                "friends": [],
                "friend_requests": [],
                "online": True,
                "current_room": None,
            }
            await users_collection.insert_one(user_data)

        # Get friends data with online status and current rooms
        friends_data = []
        for friend in user_data.get("friends", []):
            friend_data = await users_collection.find_one({"username": friend})
            if friend_data:
                friends_data.append(
                    {
                        "username": friend,
                        "online": friend_data.get("online", False),
                        "current_room": friend_data.get("current_room"),
                    }
                )

        # Prepare context for rendering
        context.update(
            {
                "username": username,
                "user_data": user_data,
                "friends": friends_data,
                "friend_requests": user_data.get("friend_requests", []),
            }
        )

        return templates.TemplateResponse("homepage.html", context)

    except Exception as e:
        context["message"] = "An error occurred while loading the homepage."
        return templates.TemplateResponse("homepage.html", context)


@app.post("/notification-settings")
async def update_notification_settings(
    settings: NotificationSettings, current_user: User = Depends(get_current_user)
):
    try:
        users_collection.update_one(
            {"username": current_user["username"]},
            {"$set": {"notification_settings": settings.dict()}},
        )
        return {"message": "Settings updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/register-fcm-token")
async def register_fcm_token(
    token: str, clear_all: bool = False, current_user: User = Depends(get_current_user)
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
    current_user: User = Depends(get_current_user),
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
                detail=f"Invalid image type. Allowed types: {', '.join(ALLOWED_IMAGE_TYPES).upper()}",
            )

        # Check file size
        await photo.seek(0)
        content = await photo.read()
        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(
                status_code=400, detail="File too large. Maximum size is 5MB"
            )

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
            {"username": username}, {"$set": {"profile_photo": photo_url}}
        )

        return {"photo_url": photo_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload photo: {str(e)}")


# Scheduler functions
async def check_inactive_users():
    threshold = datetime.utcnow() - timedelta(minutes=5)

    # Perform the query and get an async cursor
    inactive_users_cursor = heartbeats_collection.find(
        {"last_heartbeat": {"$lt": threshold}}
    )

    # Iterate over the cursor asynchronously
    async for user in inactive_users_cursor:
        # Update the user's online status
        await users_collection.update_one(
            {"username": user["username"]}, {"$set": {"online": False}}
        )

        # Delete the heartbeat record for the inactive user
        await heartbeats_collection.delete_one({"_id": user["_id"]})


# Start scheduler
@app.on_event("startup")
async def startup_event():
    await init_db()
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
        upsert=True,
    )
    users_collection.update_one(
        {"username": current_user["username"]}, {"$set": {"online": True}}
    )
    return Response(status_code=204)


@app.post("/stop_heartbeat")
async def stop_heartbeat(current_user: User = Depends(get_current_user)):
    heartbeats_collection.delete_one({"username": current_user["username"]})
    users_collection.update_one(
        {"username": current_user["username"]}, {"$set": {"online": False}}
    )
    return Response(status_code=204)


@app.get("/search_users", response_model=List[UserSuggestion])
async def search_users(q: str, current_user: User = Depends(get_current_user)):
    query = q.lower().strip()
    if not query:
        return []

    # Get current user's data for exclusion list
    current_user_data = users_collection.find_one(
        {"username": current_user["username"]}
    )
    friends_list = current_user_data.get("friends", [])

    # First, get all eligible users
    all_users = list(
        users_collection.find(
            {"username": {"$ne": current_user["username"], "$nin": friends_list}},
            {"username": 1},
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

    # Format response
    return [
        UserSuggestion(
            username=user["username"],
            profile_photo_url=f"/profile_photo/{user['username']}",
            similarity=fuzz.ratio(query, user["username"].lower()),
        )
        for user in matching_users
    ]


@app.get("/pending_room_invites")
async def pending_room_invites(current_user: User = Depends(get_current_user)):
    # Get current user's data
    user_data = await get_user_data(current_user["username"])
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    # Initialize pending_invites if it doesn't exist
    if "pending_invites" not in user_data:
        user_data["pending_invites"] = []

    return {"user_data": user_data}


async def get_user_data(username: str):
    user_data = await users_collection.find_one({"username": username})
    return user_data


@app.get("/cancel_room_invite/{username}/{room_code}")
async def cancel_room_invite(
    username: str, room_code: str, current_user: User = Depends(get_current_user)
):
    # Get the invited user's data
    friend_data = get_user_data(username)
    if not friend_data:
        raise HTTPException(status_code=404, detail="User not found")

    # Remove the invite from their room_invites
    if "room_invites" in friend_data:
        friend_data["room_invites"] = [
            inv for inv in friend_data["room_invites"] if inv.get("room") != room_code
        ]
        await users_collection.update_one({"username": username}, {"$set": friend_data})

    # Remove from current user's pending_invites
    user_data = get_user_data(current_user["username"])
    if "pending_invites" in user_data:
        user_data["pending_invites"] = [
            inv
            for inv in user_data["pending_invites"]
            if inv.get("username") != username or inv.get("room") != room_code
        ]
        await users_collection.update_one(
            {"username": current_user["username"]}, {"$set": user_data}
        )

    return {"message": f"Cancelled room invitation to {username}"}


@app.get("/invite_to_room/{username}")
async def invite_to_room(
    username: str,
    current_user: User = Depends(get_current_user),
    current_room: Optional[str] = None,
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
        raise HTTPException(
            status_code=403, detail="You can only invite friends to rooms"
        )

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
        new_invite = UserInvite(
            room=current_room,
            room_name=room_data.get("name", "Unnamed Room"),
            from_user=current_user["username"],
            profile_photo=room_data.get("profile_photo"),
        )
        friend_data["room_invites"].append(new_invite.dict())

        # Add to pending_invites for current user
        pending_invite = PendingInvite(
            username=username,
            room=current_room,
            room_name=room_data.get("name", "Unnamed Room"),
            profile_photo=room_data.get("profile_photo"),
        )
        user_data["pending_invites"].append(pending_invite.dict())

        # Save both updated data
        await users_collection.update_one({"username": username}, {"$set": friend_data})
        await users_collection.update_one(
            {"username": current_user["username"]}, {"$set": user_data}
        )
        return {"message": f"Room invitation sent to {username}!"}

    return {"message": f"{username} already has a pending invite to this room"}


@app.post("/accept_room_invite/{room_code}")
async def accept_room_invite(
    room_code: str, current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    user_data = get_user_data(username)

    # Find the invite
    room_invites = user_data.get("room_invites", [])
    invite = next((inv for inv in room_invites if inv["room"] == room_code), None)

    if not invite:
        raise HTTPException(
            status_code=404, detail="Room invite not found or already accepted"
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
    await users_collection.update_one({"username": username}, {"$set": user_data})

    # Remove pending invite from sender's data
    sender_data = get_user_data(sender_username)
    if sender_data and "pending_invites" in sender_data:
        sender_data["pending_invites"] = [
            inv
            for inv in sender_data["pending_invites"]
            if not (inv["username"] == username and inv["room"] == room_code)
        ]
        await users_collection.update_one(
            {"username": sender_username}, {"$set": sender_data}
        )

    return {"message": "Room invite accepted", "room_code": room_code}


@app.post("/decline_room_invite/{room_code}")
async def decline_room_invite(
    room_code: str, current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    user_data = get_user_data(username)

    room_invites = user_data.get("room_invites", [])
    invite = next((inv for inv in room_invites if inv["room"] == room_code), None)

    if not invite:
        raise HTTPException(
            status_code=404, detail="Room invite not found or already declined"
        )

    sender_username = invite["from"]

    # Remove the invite from recipient
    user_data["room_invites"] = [
        inv for inv in room_invites if inv["room"] != room_code
    ]
    await users_collection.update_one({"username": username}, {"$set": user_data})

    # Remove pending invite from sender's data
    sender_data = get_user_data(sender_username)
    if sender_data and "pending_invites" in sender_data:
        sender_data["pending_invites"] = [
            inv
            for inv in sender_data["pending_invites"]
            if not (inv["username"] == username and inv["room"] == room_code)
        ]
        await users_collection.update_one(
            {"username": sender_username}, {"$set": sender_data}
        )

    return {"message": "Room invite declined"}


@app.post("/join_friend_room/{friend_username}")
async def join_friend_room(
    friend_username: str, current_user: User = Depends(get_current_user)
):
    username = current_user["username"]
    user_data = users_collection.find_one({"username": username})

    if friend_username not in user_data.get("friends", []):
        raise HTTPException(status_code=403, detail="User is not in your friends list")

    friend_data = users_collection.find_one({"username": friend_username})
    friend_room = friend_data.get("current_room")

    if not friend_room:
        raise HTTPException(status_code=404, detail="Friend is not in any room")

    room_exists = rooms_collection.find_one({"_id": friend_room})
    if not room_exists:
        raise HTTPException(status_code=404, detail="Friend's room no longer exists")

    # Update user's current room
    users_collection.update_one(
        {"username": username}, {"$set": {"current_room": friend_room}}
    )

    return {"message": "Joined friend's room", "room_code": friend_room}


@app.delete("/delete_room/{room_code}")
async def delete_room(room_code: str, current_user: User = Depends(get_current_user)):
    username = current_user["username"]
    room_data = rooms_collection.find_one({"_id": room_code})

    if not room_data:
        raise HTTPException(status_code=404, detail="Room does not exist")

    if room_data["created_by"] != username:
        raise HTTPException(
            status_code=403, detail="You don't have permission to delete this room"
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
        {"$pull": {"rooms": room_code}, "$set": {"current_room": None}},
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
    room_name: Optional[str] = None,
):
    room = code
    if create:
        room = generate_unique_code()
        rooms_collection.insert_one(
            {
                "_id": room,
                "name": room_name or "Unnamed Room",
                "users": [username],
                "messages": [],
                "created_by": username,
            }
        )
    elif join:
        room_exists = rooms_collection.find_one({"_id": code})
        if not room_exists:
            raise HTTPException(status_code=404, detail="Room does not exist")

        # Add user to the room's user list
        rooms_collection.update_one({"_id": code}, {"$addToSet": {"users": username}})

    # Update user's current room and rooms list
    users_collection.update_one(
        {"username": username},
        {"$set": {"current_room": room}, "$addToSet": {"rooms": room}},
    )

    return {"room_code": room}


@app.get("/room_settings/{room_code}")
async def room_settings(room_code: str, current_user: User = Depends(get_current_user)):
    username = current_user["username"]
    room_data = rooms_collection.find_one({"_id": room_code})

    if not room_data:
        raise HTTPException(status_code=404, detail="Room does not exist")

    if username not in room_data["users"]:
        raise HTTPException(
            status_code=403, detail="You don't have access to this room"
        )

    # Get detailed user information for each room member
    room_users = []
    for user_name in room_data["users"]:
        user_data = users_collection.find_one({"username": user_name})
        if user_data:
            room_users.append(
                RoomUser(
                    username=user_name,
                    online=user_data.get("online", False),
                    current_room=user_data.get("current_room"),
                )
            )

    return {
        "room_code": room_code,
        "room_data": room_data,
        "room_users": room_users,
        "is_owner": room_data["created_by"] == username,
    }


@app.get("/search_messages/{room_code}")
async def search_messages(
    room_code: str, q: str, current_user: User = Depends(get_current_user)
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
        msg for msg in room_data["messages"] if query in msg.get("message", "").lower()
    ]

    return {"messages": matching_messages[-50:]}  # Return last 50 matches


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: str, username: str):
        await websocket.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = {}
        self.active_connections[room_id][username] = websocket

    def disconnect(self, room_id: str, username: str):
        if room_id in self.active_connections:
            self.active_connections[room_id].pop(username, None)
            if not self.active_connections[room_id]:
                self.active_connections.pop(room_id, None)

    async def broadcast_to_room(
        self, room_id: str, message: dict, exclude_user: str = None
    ):
        if room_id in self.active_connections:
            for username, connection in self.active_connections[room_id].items():
                if username != exclude_user:
                    await connection.send_json(message)


connection_manager = ConnectionManager()


async def check_and_notify_async(
    message_id: str,
    room_id: str,
    recipient_username: str,
    sender_username: str,
    message_text: str,
):
    """
    Asynchronous version of check_and_notify that handles notification delivery
    """
    try:
        # Get recipient's notification settings
        user_data = users_collection.find_one({"username": recipient_username})
        if not user_data:
            return

        # Check if user is online in the room
        is_online = (
            room_id in connection_manager.active_connections
            and recipient_username in connection_manager.active_connections[room_id]
        )

        if not is_online:
            # Get room data for notification
            room_data = rooms_collection.find_one({"_id": room_id})
            if not room_data:
                return

            notification = {
                "id": str(ObjectId()),
                "type": "message",
                "room_id": room_id,
                "room_name": room_data.get("name", "Unknown Room"),
                "message_id": message_id,
                "sender": sender_username,
                "message": message_text[:100] + "..."
                if len(message_text) > 100
                else message_text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "read": False,
            }

            # Add notification to database
            users_collection.update_one(
                {"username": recipient_username},
                {
                    "$push": {
                        "notifications": {
                            "$each": [notification],
                            "$slice": -100,  # Keep last 100 notifications
                        }
                    },
                    "$inc": {"unread_notifications": 1},
                },
            )

            # If user is online in another room, send notification through their active connection
            for room, connections in connection_manager.active_connections.items():
                if recipient_username in connections:
                    await connections[recipient_username].send_json(
                        {"type": "notification", "data": notification}
                    )
                    break

    except Exception as e:
        print(f"Error in check_and_notify_async: {str(e)}")


async def handle_message(
    websocket: WebSocket,
    room_id: str,
    data: Dict[str, Any],
    current_user: Dict[str, Any],
    manager: ConnectionManager,
):
    room_data = rooms_collection.find_one({"_id": room_id})
    if not room_data:
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
                {"_id": room_id, "messages.id": data["replyTo"]}, {"messages.$": 1}
            )
            if original_message and original_message.get("messages"):
                reply_to = {
                    "id": data["replyTo"],
                    "message": original_message["messages"][0]["message"],
                }

    content = {
        "id": str(ObjectId()),
        "name": current_user["username"],
        "message": data["data"],
        "reply_to": reply_to,
        "read_by": [current_user["username"]],
        "image": data.get("image"),
        "video": data.get("video"),
        "gif": gif_data,
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
    rooms_collection.update_one({"_id": room_id}, update_operation)

    # Broadcast to room
    await manager.broadcast_to_room(room_id, content)

    # Handle notifications (you'll need to implement this separately)
    sender_username = current_user["username"]
    room_users = room_data["users"]

    for username in room_users:
        if username != sender_username:
            await check_and_notify_async(
                message_id=content["id"],
                room_id=room_id,
                recipient_username=username,
                sender_username=sender_username,
                message_text=content["message"],
            )


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(
    websocket: WebSocket, room_id: str, current_user: User = Depends(get_current_user)
):
    await connection_manager.connect(websocket, room_id, current_user["username"])
    try:
        while True:
            data = await websocket.receive_json()

            if data["type"] == "message":
                await handle_message(websocket, room_id, data, current_user, manager)

            elif data["type"] == "user_typing":
                typing_notification = {
                    "type": "user_typing",
                    "username": current_user["username"],
                }
                await connection_manager.broadcast_to_room(
                    room_id, typing_notification, exclude_user=current_user["username"]
                )

            elif data["type"] == "find_message":
                message_id = data.get("message_id")
                if not message_id:
                    await websocket.send_json({"type": "message_found", "found": False})
                    continue

                message_data = rooms_collection.find_one(
                    {"_id": room_id, "messages.id": message_id}, {"messages.$": 1}
                )

                if not message_data:
                    await websocket.send_json({"type": "message_found", "found": False})
                    continue

                room_data = rooms_collection.find_one({"_id": room_id})
                message_index = next(
                    (
                        i
                        for i, msg in enumerate(room_data["messages"])
                        if msg["id"] == message_id
                    ),
                    None,
                )

                if message_index is None:
                    await websocket.send_json({"type": "message_found", "found": False})
                    continue

                start_index = max(0, message_index - 10)
                end_index = min(len(room_data["messages"]), message_index + 11)
                context_messages = room_data["messages"][start_index:end_index]

                messages_with_read_status = []
                for msg in context_messages:
                    msg_copy = msg.copy()
                    msg_copy["read_by"] = msg_copy.get("read_by", [])

                    # Convert datetime objects to ISO format
                    for key, value in msg_copy.items():
                        if isinstance(value, datetime):
                            msg_copy[key] = value.isoformat()

                    messages_with_read_status.append(msg_copy)

                await websocket.send_json(
                    {
                        "type": "message_found",
                        "found": True,
                        "messages": messages_with_read_status,
                        "has_more": start_index > 0
                        or end_index < len(room_data["messages"]),
                    }
                )

            elif data["type"] == "toggle_reaction":
                message_id = data["messageId"]
                emoji = data["emoji"]
                username = current_user["username"]

                message = rooms_collection.find_one(
                    {"_id": room_id, "messages.id": message_id}, {"messages.$": 1}
                )

                if message and message.get("messages"):
                    current_message = message["messages"][0]
                    current_reactions = current_message.get("reactions", {})
                    current_emoji_data = current_reactions.get(
                        emoji, {"count": 0, "users": []}
                    )

                    if username in current_emoji_data.get("users", []):
                        # Remove reaction
                        update_result = rooms_collection.update_one(
                            {"_id": room_id, "messages.id": message_id},
                            {
                                "$pull": {
                                    f"messages.$[msg].reactions.{emoji}.users": username
                                },
                                "$inc": {
                                    f"messages.$[msg].reactions.{emoji}.count": -1
                                },
                            },
                            array_filters=[{"msg.id": message_id}],
                        )

                        if current_emoji_data["count"] == 1:
                            rooms_collection.update_one(
                                {"_id": room_id, "messages.id": message_id},
                                {"$unset": {f"messages.$[msg].reactions.{emoji}": ""}},
                                array_filters=[{"msg.id": message_id}],
                            )
                    else:
                        # Add reaction
                        rooms_collection.update_one(
                            {"_id": room_id, "messages.id": message_id},
                            {
                                "$set": {
                                    f"messages.$[msg].reactions.{emoji}": {
                                        "count": current_emoji_data.get("count", 0) + 1,
                                        "users": current_emoji_data.get("users", [])
                                        + [username],
                                    }
                                }
                            },
                            array_filters=[{"msg.id": message_id}],
                        )

                    # Broadcast updated reactions
                    updated_message = rooms_collection.find_one(
                        {"_id": room_id, "messages.id": message_id}, {"messages.$": 1}
                    )

                    if updated_message and updated_message.get("messages"):
                        await connection_manager.broadcast_to_room(
                            room_id,
                            {
                                "type": "update_reactions",
                                "messageId": message_id,
                                "reactions": updated_message["messages"][0].get(
                                    "reactions", {}
                                ),
                            },
                        )

    except WebSocketDisconnect:
        connection_manager.disconnect(room_id, current_user["username"])
        await connection_manager.broadcast_to_room(
            room_id, {"type": "user_disconnected", "username": current_user["username"]}
        )
    except Exception as e:
        print(f"Error in websocket connection: {str(e)}")
        connection_manager.disconnect(room_id, current_user["username"])


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
        "unknown": "Unknown message type",
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


@app.post("/upload_video")
async def upload_video(
    video: UploadFile = File(...), current_user: User = Depends(get_current_user)
):
    if not is_valid_video(video):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid video file or size exceeds 50MB"},
        )

    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp_input:
            content = await video.read()
            temp_input.write(content)

        output_path = compress_convert_video(temp_input.name)

        filename = secure_filename(f"{str(ObjectId())}.webm")
        bucket = storage.bucket()
        blob = bucket.blob(f"room_videos/{filename}")

        blob.upload_from_filename(output_path)

        os.unlink(temp_input.name)
        os.unlink(output_path)

        video_url = blob.generate_signed_url(timedelta(days=7))

        return {"url": video_url}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


def compress_convert_video(input_file: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as temp_output:
        output_path = temp_output.name

    try:
        stream = ffmpeg.input(input_file)
        stream = ffmpeg.output(
            stream,
            output_path,
            **{
                "c:v": "libvpx-vp9",
                "crf": VIDEO_COMPRESS_CRF,
                "b:v": "1M",
                "maxrate": "1.5M",
                "bufsize": "2M",
                "c:a": "libopus",
                "b:a": "128k",
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


def is_valid_video(file: UploadFile) -> bool:
    if not file:
        return False

    file_size_mb = len(file.file.read()) / (1024 * 1024)
    file.file.seek(0)

    if file_size_mb > MAX_VIDEO_SIZE_MB:
        return False

    file_type, _ = mimetypes.guess_type(file.filename)
    return file_type in ALLOWED_VIDEO_TYPES


# HTTP endpoints
@app.get("/get_last_message/{room_code}", response_model=Optional[MessageContent])
async def get_last_message(
    room_code: str, current_user: User = Depends(get_current_user)
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
            type=get_message_type(last_message),
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
    current_user: User = Depends(get_current_user),
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
            return {"room_code": new_room_code}
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Error creating room: {str(e)}"
            )

    # Handle room joining
    if join and code:
        join_room = rooms_collection.find_one({"_id": code})
        if not join_room:
            raise HTTPException(status_code=404, detail="Room does not exist")

        try:
            rooms_collection.update_one(
                {"_id": code}, {"$addToSet": {"users": username}}
            )
            users_collection.update_one(
                {"username": username},
                {"$addToSet": {"rooms": code}, "$set": {"current_room": code}},
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
                {"username": username}, {"$set": {"current_room": code}}
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
                    user_list.append(
                        UserInfo(
                            username=user,
                            online=user_profile.get("online", False),
                            isFriend=user in user_friends,
                        )
                    )

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

                    friends_data.append(
                        UserInfo(
                            username=friend,
                            online=friend_data.get("online", False),
                            isFriend=True,
                        )
                    )

            # Get rooms with messages
            unread_messages = get_unread_messages(username)
            rooms_with_messages = []
            for room_code in user_data.get("rooms", []):
                room_info = get_room_data(room_code)
                if room_info:
                    last_message_data = prepare_room_message_data(room_info)
                    rooms_with_messages.append(
                        RoomInfo(
                            code=room_code,
                            name=room_info.get("name", "Unnamed Room"),
                            profile_photo=room_info.get("profile_photo"),
                            users=room_info.get("users", []),
                            last_message=last_message_data,
                            unread_count=unread_messages.get(str(room_code), {}).get(
                                "unread_count", 0
                            ),
                        )
                    )

            # Sort rooms by last message timestamp
            rooms_with_messages.sort(
                key=lambda room: room.last_message.timestamp, reverse=True
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
                "rooms_with_messages": rooms_with_messages,
            }

        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Error loading room data: {str(e)}"
            )

    return {"message": "No room code provided"}


@app.delete("/room/{room_code}/exit")
async def exit_room(room_code: str, current_user: User = Depends(get_current_user)):
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
            detail="Room owners cannot leave their own rooms. You must delete the room instead.",
        )

    try:
        # Update user data
        users_collection.update_one(
            {"username": username},
            {"$pull": {"rooms": room_code}, "$set": {"current_room": None}},
        )

        # Remove user from room's user list
        rooms_collection.update_one({"_id": room_code}, {"$pull": {"users": username}})

        # Add system message about user leaving
        system_message = {
            "id": str(ObjectId()),
            "name": "system",
            "message": f"{username} has left the room",
            "type": "system",
            "read_by": room_data["users"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        rooms_collection.update_one(
            {"_id": room_code}, {"$push": {"messages": system_message}}
        )

        # Broadcast the system message through WebSocket
        await connection_manager.broadcast_to_room(room_code, system_message)

        return {"message": "Successfully left the room"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error exiting room: {str(e)}")


@app.put("/room/{room_code}/name")
async def update_room_name(
    room_code: str,
    room_name: str = Form(...),
    current_user: User = Depends(get_current_user),
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
            detail="You don't have permission to update this room's name",
        )

    try:
        rooms_collection.update_one(
            {"_id": room_code}, {"$set": {"name": room_name.strip()}}
        )
        return {"message": "Room name updated successfully"}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error updating room name: {str(e)}"
        )


@app.put("/room/{room_code}/photo")
async def update_room_photo(
    room_code: str,
    photo: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
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
            {"_id": room_code}, {"$set": {"profile_photo": photo_url}}
        )

        return {"photo_url": photo_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error uploading photo: {str(e)}")


@app.post("/mark_messages_read/{room_id}")
async def mark_messages_read(
    room_id: str, data: MessageRead, current_user: User = Depends(get_current_user)
):
    username = current_user["username"]

    # Update the read status of messages in the room
    result = rooms_collection.update_many(
        {
            "_id": room_id,
            "messages": {
                "$elemMatch": {
                    "id": {"$in": data.message_ids},
                    "read_by": {"$ne": username},
                }
            },
        },
        {
            "$addToSet": {"messages.$[elem].read_by": username},
        },
        array_filters=[{"elem.id": {"$in": data.message_ids}}],
    )

    # Broadcast to room that messages have been read
    await connection_manager.broadcast_to_room(
        room_id,
        {
            "type": "messages_read",
            "reader": username,
            "message_ids": data.message_ids,
        },
    )

    return {"success": True, "modified_count": result.modified_count}


@app.get("/get_timezone")
async def get_timezone(current_user: User = Depends(get_current_user)):
    user = users_collection.find_one({"username": current_user["username"]})
    if user and "timezone" in user:
        return {"timezone": user["timezone"]}
    return {"timezone": None}  # Return None if no timezone is set

# Tenor API routes
TENOR_API_KEY = os.getenv("TENOR_API_KEY")
TENOR_BASE_URL = "https://tenor.googleapis.com/v2"


@app.get("/api/gif-categories")
async def gif_categories():
    try:
        response = requests.get(
            f"{TENOR_BASE_URL}/categories",
            params={"key": TENOR_API_KEY, "client_key": "web", "type": "featured"},
            timeout=10,  # Set a timeout in seconds
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search-suggestions")
async def search_suggestions():
    try:
        response = requests.get(
            f"{TENOR_BASE_URL}/trending_terms",
            params={"key": TENOR_API_KEY, "client_key": "web", "limit": 8},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/autocomplete-gifs")
async def autocomplete_gifs(q: str = Query("", min_length=0)):
    if not q:
        return {"results": []}

    try:
        response = requests.get(
            f"{TENOR_BASE_URL}/autocomplete",
            params={"q": q, "key": TENOR_API_KEY, "client_key": "web", "limit": 5},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search-gifs")
async def search_gifs(q: str = Query(""), limit: int = Query(16, ge=1, le=50)):
    try:
        response = requests.get(
            f"{TENOR_BASE_URL}/search",
            params={
                "q": q or "trending",
                "key": TENOR_API_KEY,
                "client_key": "web",
                "limit": limit,
                "media_filter": "minimal",
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))


def validate_gif_data(gif: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Validates and sanitizes GIF data before saving to database.
    Returns None if invalid, sanitized dict if valid.
    """
    if not gif or not isinstance(gif, dict):
        return None

    required_fields = {"url", "title"}
    if not all(field in gif for field in required_fields):
        return None

    return {
        "url": str(gif["url"]),
        "title": str(gif["title"]),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/unread_messages")
async def get_unread_messages(current_user: User = Depends(get_current_user)):
    username = current_user["username"]
    user = users_collection.find_one({"username": username})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_rooms = rooms_collection.find({"users": username})
    unread_messages = {}

    for room in user_rooms:
        room_id = str(room["_id"])
        unread_count = 0
        unread_msg_details = []

        for message in room["messages"]:
            if (
                username not in message.get("read_by", [])
                and message["name"] != username
            ):
                unread_count += 1

                # Determine message content based on type
                content = message.get("message", "")
                if "image" in message:
                    content = "📷 Image"
                elif "file" in message:
                    content = "📎 File"
                elif "gif" in message:
                    content = "🎥 GIF"

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


@app.post("/load_more_messages/{room_id}")
async def load_more_messages(
    room_id: str, data: LoadMoreMessages, current_user: User = Depends(get_current_user)
):
    room_data = rooms_collection.find_one({"_id": room_id})
    if not room_data:
        raise HTTPException(status_code=404, detail="Room not found")

    all_messages = room_data.get("messages", [])
    last_message_index = next(
        (i for i, msg in enumerate(all_messages) if msg["id"] == data.last_message_id),
        None,
    )

    if last_message_index is None:
        raise HTTPException(status_code=404, detail="Message not found")

    # Load 20 more messages before the last loaded message
    start_index = max(0, last_message_index - 20)
    messages_to_send = all_messages[start_index:last_message_index]

    messages_with_read_status = []
    for msg in messages_to_send:
        msg_copy = msg.copy()
        msg_copy["read_by"] = msg_copy.get("read_by", [])
        for key, value in msg_copy.items():
            if isinstance(value, datetime):
                msg_copy[key] = value.isoformat()
        messages_with_read_status.append(msg_copy)

    return {"messages": messages_with_read_status, "has_more": start_index > 0}


@app.put("/edit_message/{room_id}/{message_id}")
async def edit_message(
    room_id: str,
    message_id: str,
    new_text: str,
    current_user: User = Depends(get_current_user),
):
    username = current_user["username"]

    result = rooms_collection.update_one(
        {
            "_id": room_id,
            "messages": {
                "$elemMatch": {
                    "id": message_id,
                    "name": username,  # Ensure only message owner can edit
                }
            },
        },
        {
            "$set": {
                "messages.$[elem].message": new_text,
                "messages.$[elem].edited": True,
            }
        },
        array_filters=[{"elem.id": message_id}],
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Message not found or unauthorized")

    # Broadcast the edit to all users in the room
    await connection_manager.broadcast_to_room(
        room_id, {"type": "edit_message", "messageId": message_id, "newText": new_text}
    )

    return {"success": True}


@app.delete("/delete_message/{room_id}/{message_id}")
async def delete_message(
    room_id: str, message_id: str, current_user: User = Depends(get_current_user)
):
    username = current_user["username"]

    result = rooms_collection.update_one(
        {"_id": room_id}, {"$pull": {"messages": {"id": message_id, "name": username}}}
    )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=404,
            detail="Message not found or you don't have permission to delete it",
        )

    # Broadcast deletion to room
    await connection_manager.broadcast_to_room(
        room_id, {"type": "delete_message", "messageId": message_id}
    )

    return {"success": True}


async def handle_typing_notification(room_id: str, username: str, is_typing: bool):
    await connection_manager.broadcast_to_room(
        room_id,
        {"type": "typing", "name": username, "isTyping": is_typing},
        exclude_user=username,
    )


# Database initialization function
async def init_db():
    # Create indexes if they don't exist
    await users_collection.create_index([("username", 1)], unique=True)
    await rooms_collection.create_index([("users", 1)])
    await rooms_collection.create_index([("messages.id", 1)])


# Main entry point
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=True,  # Enable auto-reload during development
        workers=1,  # Adjust based on your needs
    )
