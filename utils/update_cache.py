import threading
import time


RECENT_UPDATE_TTL_SECONDS = 5

_recent_updates_cache = {}
_recent_updates_lock = threading.Lock()


def mark_update_made(item_id: str, column_id: str):
    """Record an update made by this server so follow-up webhooks can be ignored."""
    key = (str(item_id), str(column_id))
    with _recent_updates_lock:
        now = time.time()
        _recent_updates_cache[key] = now
        expired_keys = [
            cached_key
            for cached_key, timestamp in _recent_updates_cache.items()
            if now - timestamp > RECENT_UPDATE_TTL_SECONDS * 2
        ]
        for cached_key in expired_keys:
            del _recent_updates_cache[cached_key]


def is_self_triggered_update(item_id: str, column_id: str) -> bool:
    """Return true when a webhook likely came from a recent server-side update."""
    key = (str(item_id), str(column_id))
    with _recent_updates_lock:
        timestamp = _recent_updates_cache.get(key)
        if timestamp is None:
            return False
        if time.time() - timestamp < RECENT_UPDATE_TTL_SECONDS:
            return True
        del _recent_updates_cache[key]
        return False
