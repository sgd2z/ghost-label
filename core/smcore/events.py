"""In-process pub/sub feeding the UI's server-sent events stream."""

from __future__ import annotations

import json
import queue
import threading
import time


class Events:
    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = []
        self.watchers = []     # in-process callbacks(event_type), e.g. the queue view

    def watch(self, fn):
        self.watchers.append(fn)

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def emit(self, event_type: str, **data):
        message = json.dumps({"type": event_type, "at": time.time(), **data}, ensure_ascii=False, default=str)
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(message)
            except queue.Full:
                pass
        for fn in list(self.watchers):
            try:
                fn(event_type)
            except Exception:  # noqa: BLE001 — a watcher never breaks the sender
                pass
