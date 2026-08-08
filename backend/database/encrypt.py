import os
import logging
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# Fallback development key (32 URL-safe base64-encoded bytes)
DEFAULT_DEV_KEY = "kX4W5J8-WlhE1tqf7xG_Y6D-63q5XJc0Y3FfD6H5K8Q="

# Determine runtime environment
ENV = os.getenv("ENV", "development")

# Load encryption key from environment variable
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

if not ENCRYPTION_KEY:
    if ENV == "production":
        raise RuntimeError(
            "ENCRYPTION_KEY environment variable is required in production! "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    # Development / staging: allow fallback key with a warning
    logger.warning(
        "WARNING: ENCRYPTION_KEY environment variable is not set! "
        "Using a stable fallback key for local development. "
        "This is NOT safe for production."
    )
    ENCRYPTION_KEY = DEFAULT_DEV_KEY
    os.environ["ENCRYPTION_KEY"] = ENCRYPTION_KEY

try:
    fernet = Fernet(ENCRYPTION_KEY.encode())
except Exception as e:
    logger.error(f"Failed to initialize Fernet with ENCRYPTION_KEY: {e}. Falling back to default dev key.")
    ENCRYPTION_KEY = DEFAULT_DEV_KEY
    fernet = Fernet(ENCRYPTION_KEY.encode())

def encrypt_value(val: str) -> str:
    """Encrypt a string value using Fernet symmetric encryption."""
    if not val:
        return val
    try:
        return fernet.encrypt(val.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"Encryption error: {e}")
        return val

def decrypt_value(val: str) -> str:
    """Decrypt a Fernet encrypted string value. Returns original if not encrypted or fails."""
    if not val:
        return val
    try:
        return fernet.decrypt(val.encode("utf-8")).decode("utf-8")
    except Exception:
        # Fallback if the value is not encrypted or uses an old key
        return val
