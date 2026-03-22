from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    expires_at: datetime


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int, max_items: int = 256) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._items: dict[str, CacheEntry[T]] = {}
        self._lock = Lock()

    def get(self, key: str) -> T | None:
        with self._lock:
            entry = self._items.get(key)
            if not entry:
                return None
            if entry.expires_at <= datetime.now(timezone.utc):
                self._items.pop(key, None)
                return None
            return entry.value

    def set(self, key: str, value: T) -> None:
        with self._lock:
            if len(self._items) >= self.max_items:
                oldest_key = min(self._items, key=lambda item_key: self._items[item_key].expires_at)
                self._items.pop(oldest_key, None)
            self._items[key] = CacheEntry(
                value=value,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds),
            )

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
