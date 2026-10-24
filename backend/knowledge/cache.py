import time
import threading
import json
import os

class TTLCache:
    def __init__(self, maxsize: int = 1000, ttl_seconds: int = 300):
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self.cache = {}
        self.expiry = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        """Retrieve an item from cache if it exists and hasn't expired."""
        with self._lock:
            if key in self.cache:
                if time.time() < self.expiry[key]:
                    return self.cache[key]
                else:
                    self.delete(key)
            return None

    def set(self, key: str, value):
        """Add item to cache, evicting oldest/expired if limit is reached."""
        with self._lock:
            # Clean up any expired keys first to free space
            now = time.time()
            expired_keys = [k for k, exp in self.expiry.items() if now >= exp]
            for k in expired_keys:
                self.delete(k)

            if len(self.cache) >= self.maxsize:
                # Evict the item that expires earliest
                earliest_expiry_key = min(self.expiry, key=self.expiry.get, default=None)
                if earliest_expiry_key:
                    self.delete(earliest_expiry_key)

            self.cache[key] = value
            self.expiry[key] = time.time() + self.ttl_seconds

    def delete(self, key: str):
        self.cache.pop(key, None)
        self.expiry.pop(key, None)


class RedisCache:
    def __init__(self, host="localhost", port=6379, db=0, ttl_seconds=300, redis_url=None, key_prefix=""):
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self.is_connected = False
        self.fallback = TTLCache(ttl_seconds=ttl_seconds)
        self.client = None

        url = redis_url or os.environ.get("REDIS_URL", "")
        # If url is provided and is a valid remote cloud Redis (starts with redis:// or rediss://)
        # and not plain localhost:
        if url and (url.startswith("rediss://") or (url.startswith("redis://") and "localhost" not in url and "127.0.0.1" not in url)):
            try:
                import redis
                # Force database 0 for Upstash Redis / cloud compatibility
                clean_url = url
                if clean_url.rstrip("/").split("/")[-1].isdigit():
                    clean_url = "/".join(clean_url.rstrip("/").split("/")[:-1]) + "/0"

                self.client = redis.from_url(
                    clean_url,
                    socket_connect_timeout=2.0,
                    socket_timeout=2.0,
                    retry_on_timeout=True
                )
                self.client.ping()
                self.is_connected = True
                print(f"[REDIS] Connected successfully to Cloud Redis ({clean_url.split('@')[-1] if '@' in clean_url else 'cloud'})")
                return
            except Exception as e:
                print(f"[REDIS] Remote Redis connection failed ({e}). Gracefully falling back to memory TTLCache.")
                return

        # If on serverless without remote cloud Redis configured, skip local host socket check
        if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            return

        # Map localhost explicitly to IPv4 loopback to avoid Windows IPv6 dual-stack DNS check delays
        resolved_host = "127.0.0.1" if host == "localhost" else host

        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)  # Strict 500ms timeout
        try:
            s.connect((resolved_host, port))
            s.close()

            import redis
            self.client = redis.Redis(host=resolved_host, port=port, db=0, socket_connect_timeout=1.0)
            self.client.ping()
            self.is_connected = True
            print(f"[REDIS] Successfully connected to Redis on {resolved_host}:{port}")
        except Exception as e:
            print(f"[REDIS] Redis is unavailable on {resolved_host}:{port} ({e}). Gracefully falling back to local memory cache.")

    def _format_key(self, key: str) -> str:
        return f"{self.key_prefix}{key}" if self.key_prefix else key

    def get(self, key: str):
        full_key = self._format_key(key)
        if self.is_connected and self.client:
            try:
                val = self.client.get(full_key)
                if val:
                    try:
                        return json.loads(val.decode("utf-8"))
                    except Exception:
                        return val.decode("utf-8")
            except Exception as e:
                print(f"[REDIS ERROR] get key '{full_key}' failed: {e}. Using fallback cache.")
                return self.fallback.get(full_key)
        return self.fallback.get(full_key)

    def set(self, key: str, value):
        full_key = self._format_key(key)
        if self.is_connected and self.client:
            try:
                serialized = json.dumps(value) if not isinstance(value, (str, bytes)) else value
                self.client.set(full_key, serialized, ex=self.ttl_seconds)
                return
            except Exception as e:
                print(f"[REDIS ERROR] set key '{full_key}' failed: {e}. Using fallback cache.")
                self.fallback.set(full_key, value)
        else:
            self.fallback.set(full_key, value)

    def delete(self, key: str):
        full_key = self._format_key(key)
        if self.is_connected and self.client:
            try:
                self.client.delete(full_key)
                return
            except Exception as e:
                print(f"[REDIS ERROR] delete key '{full_key}' failed: {e}. Using fallback cache.")
                self.fallback.delete(full_key)
        else:
            self.fallback.delete(full_key)


# ── Global Cache Instances ──────────────────────────────────────────
from config import settings

# Redis connection host defaults to localhost
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# Embedding cache: "patient_uid:query" -> embedding vector
embedding_cache = RedisCache(ttl_seconds=settings.CACHE_EMBEDDING_TTL, redis_url=settings.REDIS_URL, key_prefix="emb:")

# Retrieval cache: "patient_uid:query" -> string response list of docs
retrieval_cache = RedisCache(ttl_seconds=settings.CACHE_RETRIEVAL_TTL, redis_url=settings.REDIS_URL, key_prefix="ret:")

# Response cache: "patient_uid:query" -> LLM response (shortest TTL for freshness)
response_cache = RedisCache(ttl_seconds=settings.CACHE_RESPONSE_TTL, redis_url=settings.REDIS_URL, key_prefix="")


# ── Response Cache Helper Functions ────────────────────────────────
import hashlib
import re

# Intents whose responses are inherently user-specific or time-sensitive
# and must NEVER be served from a shared cache.
_UNCACHEABLE_INTENTS = frozenset({
    "PROFILE_QUERY",   # Patient bills, records, personal info
    "BOOKING_INTENT",  # Appointment availability changes in real time
    "DIRECT_ACTION",   # Login, registration — stateful side-effects
})

def _normalize_query(query: str) -> str:
    """Normalize user query for semantic caching (lowercase, strip extra spaces/punctuation)."""
    q = query.lower().strip()
    q = re.sub(r'[^\w\s]', '', q)
    return re.sub(r'\s+', ' ', q)

def _classify_intent_for_cache(query: str) -> str:
    """Lightweight intent classification to scope cache keys.
    Imported lazily to avoid circular imports at module load time."""
    try:
        from agent.intent_classifier import classify_intent
        result = classify_intent(query)
        return result.get("intent", "GENERAL_KNOWLEDGE")
    except Exception:
        return "GENERAL_KNOWLEDGE"

def get_response_cache(query: str, patient_uid: str = "guest") -> str | None:
    """Retrieve cached response if available for identical/similar queries.
    
    Args:
        query: The raw user message.
        patient_uid: Authenticated patient UID (used for cache key isolation).
    
    Returns:
        Cached response text, or None on cache miss / uncacheable intent.
    """
    norm_q = _normalize_query(query)
    if not norm_q or len(norm_q) < 3:
        return None

    intent = _classify_intent_for_cache(query)

    # Never serve stale cached results for user-specific intents
    if intent in _UNCACHEABLE_INTENTS:
        return None

    query_hash = hashlib.md5(norm_q.encode('utf-8')).hexdigest()
    key = f"resp:{intent}:{query_hash}"
    hit = response_cache.get(key)
    if hit:
        print(f"[CACHE HIT] Response cache matched key resp:{intent}:{query_hash[:8]}...")
    return hit

def set_response_cache(query: str, response_text: str, patient_uid: str = "guest"):
    """Store generated LLM response in cache.
    
    Args:
        query: The raw user message.
        response_text: The generated LLM response to cache.
        patient_uid: Authenticated patient UID (used for cache key isolation).
    """
    norm_q = _normalize_query(query)
    if not norm_q or len(norm_q) < 3 or not response_text or len(response_text) < 10:
        return

    intent = _classify_intent_for_cache(query)

    # Do not cache user-specific or stateful intents
    if intent in _UNCACHEABLE_INTENTS:
        return

    # Do not cache error responses or emergency warnings
    resp_lower = response_text.lower()
    if any(err_txt in resp_lower for err_txt in [
        "unavailable", "error", "emergency medical attention",
        "rephrasing", "apologize", "try again"
    ]):
        return

    query_hash = hashlib.md5(norm_q.encode('utf-8')).hexdigest()
    key = f"resp:{intent}:{query_hash}"
    response_cache.set(key, response_text)
    print(f"[CACHE SET] Stored response for key resp:{intent}:{query_hash[:8]}...")
