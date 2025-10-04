# src/config.py
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from a .env file if present (useful for local dev)
env_path = Path('.') / '.env'
if env_path.exists():
    load_dotenv(env_path)

# --- General ---
PYTHON_VERSION = os.getenv("PYTHON_VERSION", "3.11")

# --- Hostaway API ---
HOSTAWAY_API_BASE = os.getenv("HOSTAWAY_API_BASE", "https://api.hostaway.com/v1")
HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_ACCESS_TOKEN = os.getenv("HOSTAWAY_ACCESS_TOKEN")

# --- OpenAI ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# --- Google APIs ---
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
GOOGLE_DISTANCE_MATRIX_API_KEY = os.getenv("GOOGLE_DISTANCE_MATRIX_API_KEY")

# --- Slack ---
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
SLACK_INTERACT_URL = os.getenv("SLACK_INTERACT_URL")

# --- Business Logic ---
DEFAULT_CHECKIN_TIME = os.getenv("DEFAULT_CHECKIN_TIME", "16:00")
DEFAULT_CHECKOUT_TIME = os.getenv("DEFAULT_CHECKOUT_TIME", "11:00")
EARLY_CHECKIN_FEE = float(os.getenv("EARLY_CHECKIN_FEE", 0))
LATE_CHECKOUT_FEE = float(os.getenv("LATE_CHECKOUT_FEE", 0))

# --- Paths ---
LEARNING_DB_PATH = os.getenv("LEARNING_DB_PATH", "/var/data/learning.db")
MEMORY_YAML_PATH = os.getenv("MEMORY_YAML_PATH", "/var/data/memory.yaml")

# --- Feature Flags ---
SMART_AUTOREPLY = bool(int(os.getenv("SMART_AUTOREPLY", "1")))
SHADOW_MODE = bool(int(os.getenv("SHADOW_MODE", "0")))
SHOW_NEW_GUEST_TAG = bool(int(os.getenv("SHOW_NEW_GUEST_TAG", "0")))

# --- Weather API (optional future integration) ---
# We'll add something like OPENWEATHER_API_KEY later if needed

def validate_config():
    """Check that required environment variables are set."""
    required_vars = {
        "HOSTAWAY_ACCESS_TOKEN": HOSTAWAY_ACCESS_TOKEN,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
        "SLACK_SIGNING_SECRET": SLACK_SIGNING_SECRET,
    }
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

