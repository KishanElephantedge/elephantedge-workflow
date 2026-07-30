import json
import time

import redis

# Redis runs INSIDE this same container (started by the Dockerfile's CMD before uvicorn) --
# not a separate Render service, deliberately, so caching never adds a second cold-start
# surface. It's a pure cache: if it's ever unreachable, every function here degrades to a
# silent no-op (cache miss / skipped write) rather than raising, so a cache outage can never
# turn into a request failure -- the caller just falls through to querying the DB directly.
_client = redis.Redis(host="localhost", port=6379, decode_responses=True, socket_connect_timeout=1)

# Sorted set of cache keys that have been read recently, scored by last-read unix time --
# lets the background refresher find "what's actually being viewed" without scanning all of
# Redis, and lets it forget batches nobody's looked at in a while instead of refreshing forever.
ACTIVE_KEYS_SET = "active_cache_keys"
ACTIVE_WINDOW_SECONDS = 30 * 60


def cache_get(key: str):
    try:
        raw = _client.get(key)
    except redis.RedisError:
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def cache_set(key: str, value, ttl_seconds: int):
    try:
        _client.set(key, json.dumps(value), ex=ttl_seconds)
    except redis.RedisError:
        pass


def mark_active(key: str):
    try:
        _client.zadd(ACTIVE_KEYS_SET, {key: time.time()})
    except redis.RedisError:
        pass


def get_batch_version(batch_id: int) -> int:
    """The cache key for a batch includes this version number, so bumping it (on any write
    that changes the batch's companies/contacts/scores) makes every previously-cached page for
    that batch unreachable immediately -- no need to enumerate and delete the old keys one by
    one, they just age out on their own TTL."""
    try:
        raw = _client.get(f"batch_version:{batch_id}")
        return int(raw) if raw is not None else 0
    except (redis.RedisError, ValueError, TypeError):
        return 0


def bump_batch_version(batch_id: int):
    try:
        _client.incr(f"batch_version:{batch_id}")
    except redis.RedisError:
        pass


def active_keys() -> list[str]:
    """Keys read within the last ACTIVE_WINDOW_SECONDS -- also prunes anything older, so the
    set doesn't grow forever with batches nobody's viewing anymore."""
    now = time.time()
    try:
        _client.zremrangebyscore(ACTIVE_KEYS_SET, 0, now - ACTIVE_WINDOW_SECONDS)
        return _client.zrangebyscore(ACTIVE_KEYS_SET, now - ACTIVE_WINDOW_SECONDS, now)
    except redis.RedisError:
        return []
