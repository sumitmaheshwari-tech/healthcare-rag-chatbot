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
    def __init__(self, host="localhost", port=6379, db=0, ttl_seconds=300):
        self.ttl_seconds = ttl_seconds
        self.is_connected = False
        self.fallback = TTLCache(ttl_seconds=ttl_seconds)
        self.client = None

        # Map localhost explicitly to IPv4 loopback to avoid Windows IPv6 dual-stack DNS check delays
        resolved_host = "127.0.0.1" if host == "localhost" else host

        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)  # Strict 500ms timeout
        try:
            s.connect((resolved_host, port))
            s.close()

            import redis
            self.client = redis.Redis(host=resolved_host, port=port, db=db, socket_connect_timeout=1.0)
            self.client.ping()
            self.is_connected = True
            print(f"[REDIS] Successfully connected to Redis on {resolved_host}:{port}")
        except Exception as e:
            print(f"[REDIS] Redis is unavailable on {resolved_host}:{port} ({e}). Gracefully falling back to local memory cache.")

    def get(self, key: str):
        if self.is_connected and self.client:
            try:
                val = self.client.get(key)
                if val:
                    try:
                        return json.loads(val.decode("utf-8"))
                    except Exception:
                        return val.decode("utf-8")
            except Exception as e:
                print(f"[REDIS ERROR] get key '{key}' failed: {e}. Using fallback cache.")
                return self.fallback.get(key)
        return self.fallback.get(key)

    def set(self, key: str, value):
        if self.is_connected and self.client:
            try:
                serialized = json.dumps(value) if not isinstance(value, (str, bytes)) else value
                self.client.setex(key, self.ttl_seconds, serialized)
                return
            except Exception as e:
                print(f"[REDIS ERROR] set key '{key}' failed: {e}. Using fallback cache.")
                self.fallback.set(key, value)
        else:
            self.fallback.set(key, value)

    def delete(self, key: str):
        if self.is_connected and self.client:
            try:
                self.client.delete(key)
                return
            except Exception as e:
                print(f"[REDIS ERROR] delete key '{key}' failed: {e}. Using fallback cache.")
                self.fallback.delete(key)
        else:
            self.fallback.delete(key)


# ── Global Cache Instances ──────────────────────────────────────────
from config import settings

# Redis connection host defaults to localhost
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# Embedding cache: "patient_uid:query" -> embedding vector
embedding_cache = RedisCache(host="127.0.0.1", port=6379, db=0, ttl_seconds=settings.CACHE_EMBEDDING_TTL)

# Retrieval cache: "patient_uid:query" -> string response list of docs
retrieval_cache = RedisCache(host="127.0.0.1", port=6379, db=1, ttl_seconds=settings.CACHE_RETRIEVAL_TTL)

# Response cache: "patient_uid:query" -> LLM response (shortest TTL for freshness)
response_cache = RedisCache(host="127.0.0.1", port=6379, db=2, ttl_seconds=settings.CACHE_RESPONSE_TTL)
