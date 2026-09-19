#!/usr/bin/env python3
import os
import re
import hashlib
import json
import shutil
import datetime
import random
import string
from typing import Dict, Optional, Tuple, List

# Constants
ROOT_DIR = "/los"
SALT_LENGTH = 16

def is_valid_username(username: str) -> bool:
    """
    Validate a username format.
    Username should start with a letter and only contain lowercase letters, numbers, hyphens, or underscores.
    """
    return bool(re.match(r'^[a-z][a-z0-9_-]{2,29}$', username))

def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """Hash a password with a salt using PBKDF2."""
    if salt is None:
        salt = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(SALT_LENGTH))
    
    # Use PBKDF2 with SHA-256, 100,000 iterations
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    hashed_password = key.hex()
    
    return hashed_password, salt

def verify_password(password: str, hashed_password: str, salt: str) -> bool:
    """Verify a password against a hash and salt."""
    new_hash, _ = hash_password(password, salt)
    return new_hash == hashed_password

def create_user_directories(username: str) -> str:
    """Create required directories for a new user using the skeleton template."""
    user_home = os.path.join(ROOT_DIR, username)
    skeleton_dir = os.path.join(ROOT_DIR, "sys", "skeleton")
    
    # Create user home directory if it doesn't exist
    if not os.path.exists(user_home):
        os.makedirs(user_home, exist_ok=True)
    
    # Copy skeleton directory structure to user home
    for root, dirs, files in os.walk(skeleton_dir):
        # Calculate relative path from skeleton root
        rel_path = os.path.relpath(root, skeleton_dir)
        # Create corresponding directory in user home
        if rel_path == '.':
            # Skip the skeleton root itself
            target_dir = user_home
        else:
            target_dir = os.path.join(user_home, rel_path)
            
        if not os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)
        
        # Copy files
        for file in files:
            source_file = os.path.join(root, file)
            target_file = os.path.join(target_dir, file)
            
            if not os.path.exists(target_file):
                shutil.copy2(source_file, target_file)
    
    # Create entities file if not copied from skeleton
    entities_file = os.path.join(user_home, 'entities')
    if not os.path.exists(entities_file):
        with open(entities_file, 'w') as f:
            f.write("# Sub-entities for this entity\n# Format: one entity name per line\n")
    
    return user_home

def create_los_user(username: str, password: str, full_name: str = "", email: str = "") -> bool:
    """
    Create a LOS user (without creating a system user).
    """
    try:
        # Create home directory with skeleton template
        user_home = create_user_directories(username)
        
        # Create credentials file
        hashed_pass, salt = hash_password(password)
        credentials = {
            "username": username,
            "full_name": full_name,
            "email": email,
            "salt": salt,
            "hashed_password": hashed_pass,
            "created_at": datetime.datetime.now().isoformat()
        }
        
        creds_file = os.path.join(user_home, '.credentials')
        with open(creds_file, 'w') as f:
            json.dump(credentials, f, indent=2)
        
        # Ensure credentials file has limited permissions
        os.chmod(creds_file, 0o600)  # Read/write only for the owner
        
        # Add user to main entities file
        entities_file = os.path.join(ROOT_DIR, 'entities')
        if os.path.exists(entities_file):
            with open(entities_file, 'r') as f:
                content = f.read()
            
            # Check if username is already in entities file
            if username not in content:
                with open(entities_file, 'a') as f:
                    f.write(f"{username}\n")
        
        return True
    
    except Exception as e:
        print(f"Error creating LOS user: {e}")
        return False

def update_los_password(username: str, password: str) -> bool:
    """Update the LOS password in .credentials file"""
    try:
        user_home = os.path.join(ROOT_DIR, username)
        creds_file = os.path.join(user_home, '.credentials')
        
        if not os.path.exists(creds_file):
            print(f"Error: Credentials file not found for user '{username}'")
            return False
        
        # Read current credentials
        with open(creds_file, 'r') as f:
            creds = json.load(f)
        
        # Update password hash
        hashed_pass, salt = hash_password(password)
        creds["salt"] = salt
        creds["hashed_password"] = hashed_pass
        
        # Write updated credentials
        with open(creds_file, 'w') as f:
            json.dump(creds, f, indent=2)
        
        # Ensure credentials file has limited permissions
        os.chmod(creds_file, 0o600)  # Read/write only for the owner
        
        return True
    
    except Exception as e:
        print(f"Error updating LOS password: {e}")
        return False

def authenticate_user(username: str, password: str) -> bool:
    """Authenticate a user."""
    try:
        # Check credentials file
        user_home = os.path.join(ROOT_DIR, username)
        creds_file = os.path.join(user_home, '.credentials')
        
        if not os.path.exists(creds_file):
            return False
        
        with open(creds_file, 'r') as f:
            creds = json.load(f)
        
        return verify_password(password, creds["hashed_password"], creds["salt"])
    
    except Exception as e:
        print(f"Error authenticating user: {e}")
        return False

def get_user_data_paths(username: str) -> Dict[str, str]:
    """Get the paths to the user's data files."""
    user_home = os.path.join(ROOT_DIR, username)
    
    return {
        'calendar': os.path.join(user_home, 'data', 'calendar', 'calendar.txt'),
        'todo': os.path.join(user_home, 'data', 'todo', 'todo.txt'),
        'crontab': os.path.join(user_home, 'data', 'cron', 'crontab.txt')
    }

def get_user_info(username: str) -> Optional[Dict]:
    """Get info about a user."""
    try:
        user_home = os.path.join(ROOT_DIR, username)
        creds_file = os.path.join(user_home, '.credentials')
        
        if not os.path.exists(creds_file):
            return None
        
        with open(creds_file, 'r') as f:
            creds = json.load(f)
        
        # Remove sensitive info
        if "hashed_password" in creds:
            del creds["hashed_password"]
        if "salt" in creds:
            del creds["salt"]
        
        return creds
    
    except Exception as e:
        print(f"Error getting user info: {e}")
        return None

def ensure_user_files_exist(username: str) -> None:
    """Ensure the user's data files exist."""
    paths = get_user_data_paths(username)
    
    for file_path in paths.values():
        directory = os.path.dirname(file_path)
        
        # Create directory if it doesn't exist
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        
        # Create file if it doesn't exist
        if not os.path.exists(file_path):
            with open(file_path, 'w') as f:
                pass  # Create empty file

def list_users() -> List[str]:
    """List all users with home directories in the ROOT_DIR."""
    users = []
    try:
        for entry in os.listdir(ROOT_DIR):
            user_home = os.path.join(ROOT_DIR, entry)
            creds_file = os.path.join(user_home, '.credentials')
            
            if os.path.isdir(user_home) and os.path.exists(creds_file):
                users.append(entry)
    except Exception as e:
        print(f"Error listing users: {e}")
    
    return users

def delete_los_user(username: str) -> bool:
    """Delete a LOS user (without affecting system users)."""
    try:
        user_home = os.path.join(ROOT_DIR, username)
        
        # Check if user exists
        if not os.path.exists(user_home):
            print(f"Error: User '{username}' does not exist")
            return False
        
        # Remove from entities file
        entities_file = os.path.join(ROOT_DIR, 'entities')
        if os.path.exists(entities_file):
            with open(entities_file, 'r') as f:
                lines = f.readlines()
            
            with open(entities_file, 'w') as f:
                for line in lines:
                    if line.strip() != username:
                        f.write(line)
        
        # Remove user directory
        shutil.rmtree(user_home, ignore_errors=True)
        
        return True
    except Exception as e:
        print(f"Error deleting LOS user: {e}")
        return False
