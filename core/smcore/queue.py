"""One view of everything waiting on an AI resource: chat requests (per server and model), image
requests, the music engine's recording queue and the background task pool. Bookkeeping only: it
reads what the provider, the studio and the job manager already know, groups items by the
resource they wait on, numbers them in the order that resource will take them, and remembers the
last few that ended. Pages show where their own items stand; nothing here decides anything.

Who an AI request is for comes from `owner(...)`, a per-thread context the code that makes the
request sets (an album being written, a cover, a song's lyrics, a job)."""

from __future__ import annotations

import threading
import time
from collections import deque

REFS = ("artistId", "albumId", "trackId", "setId", "briefId", "jobId")

_ctx = threading.local()


class owner:
    """`with owner(kind="cover", label="Cover for X", albumId=...):` tags the AI requests this
    thread makes. Nested contexts add to the outer one (refs merge, the inner kind/label win)."""

    def __init__(self, **fields):
        self.fields = {k: v for k, v in fields.items() if v is not None}

    def __enter__(self):
        self.before = getattr(_ctx, "value", None)
        merged = dict(self.before or {})
        merged.update(self.fields)
        _ctx.value = merged
        return merged

    def __exit__(self, *exc):
        _ctx.value = self.before


def current_owner() -> dict:
    return dict(getattr(_ctx, "value", None) or {})


def refs_of(data) -> dict:
    data = dict(data or {})
    if data.get("sourceSetId") and not data.get("setId"):
        data["setId"] = data["sourceSetId"]
    return {k: data[k] for k in REFS if data.get(k)}


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


class GlobalQueue:
    RECENT = 20

    def __init__(self, provider, jobs, studio, settings, events, db=None, log=print):
        self.provider, self.jobs, self.studio = provider, jobs, studio
        self.settings, self.events, self.db, self.log = settings, events, db, log
        self.lock = threading.Lock()
        self.recent = deque(maxlen=self.RECENT)
        self.seen = {}           # id -> last active item (to record how it ended)
        self._timer = None
        self._last_emit = 0.0

    # MARK: resources

    def engine_label(self) -> str:
        from smcore import lemonade
        studio = self.studio
        if studio is not None and studio.current and studio.builtin is not None and studio.active is studio.builtin:
            return "Music engine · ACE-Step (built-in)"
        if lemonade.selected(self.settings):
            return f"Music engine · Lemonade ({lemonade.model(self.settings)})"
        eng = self.settings.section("engine")
        if eng.get("kind") == "custom":
            return "Music engine · custom engine"
        return "Music engine · ACE-Step"

    # MARK: snapshot

    def _calls(self):
        out = []
        for c in self.provider.activity():
            kind = c.get("kind") or "chat"
            resource = f"{kind}:{c.get('base')}|{c.get('model')}"
            label = ("Images · " if kind == "image" else "Chat · ") + str(c.get("model"))
            who = c.get("owner") or {}
            purpose = c.get("purpose") or ("image" if kind == "image" else "")
            text = who.get("label") or (purpose[:1].upper() + purpose[1:] if purpose else "AI request")
            if purpose and who.get("label") and purpose not in who["label"].lower():
                text = f"{who['label']} · {purpose}"
            state = "queued" if c.get("state") == "queued" else "running"
            item = {"id": "call:" + c["id"], "source": "call", "kind": purpose or who.get("kind") or kind,
                    "label": text, "refs": refs_of(who), "resource": resource, "resourceLabel": label,
                    "model": c.get("model"), "state": state, "phase": c.get("state"),
                    "created": c["started"], "started": c.get("sent") if state == "running" else None,
                    "lane": c.get("lane") or "background", "progress": None}
            cancel = who.get("cancel")
            if cancel:
                item["cancel"] = dict(cancel)
            out.append(item)
        # A request someone waits on has its own slot, so it goes ahead of background work.
        return out

    def _recordings(self):
        studio = self.studio
        if studio is None:
            return []
        with studio.lock:
            current = dict(studio.current) if studio.current else None
        try:
            upcoming = studio.upcoming(50)
        except Exception:  # noqa: BLE001
            upcoming = []
        label = self.engine_label()
        ids = ([current["trackId"]] if current else []) + [t for t in upcoming if not current or t != current["trackId"]]
        rows = {}
        if ids and self.db is not None:
            marks = ",".join("?" * len(ids))
            for r in self.db.all("SELECT t.id, t.title, t.album_id, t.artist_id, t.status, al.title AS album "
                                 f"FROM tracks t LEFT JOIN albums al ON al.id = t.album_id WHERE t.id IN ({marks})", ids):
                rows[r["id"]] = r
        with studio.lock:
            requested = set(studio.needs) | set(studio.requested)
        out = []
        for i, tid in enumerate(ids):
            r = rows.get(tid) or {}
            running = bool(current) and i == 0
            item = {"id": "rec:" + tid, "source": "recording", "kind": "recording",
                    "label": f"Recording “{r.get('title') or tid}”",
                    "refs": {k: v for k, v in (("trackId", tid), ("albumId", r.get("album_id")),
                                                ("artistId", r.get("artist_id"))) if v},
                    "resource": "engine", "resourceLabel": label, "state": "running" if running else "queued",
                    "created": None, "started": (current.get("started") or 0) / 1000 if running else None,
                    "requested": tid in requested, "cancel": {"what": "recording", "label": r.get("title") or "this song"}}
            if running:
                item["phase"] = current.get("stage")
                if current.get("step") and current.get("steps"):
                    item["progress"] = {"step": current["step"], "steps": current["steps"],
                                        "fraction": current.get("overall") or current["step"] / current["steps"]}
                elif current.get("overall"):
                    item["progress"] = {"fraction": current["overall"]}
                item["detail"] = current.get("detail")
            out.append(item)
        return out

    def _jobs(self):
        out = []
        with self.jobs.lock:
            active = list(self.jobs.running)
        if not active:
            return out
        for row in self.jobs.list(200):
            if row["id"] not in active or row["status"] not in ("queued", "running"):
                continue
            item = {"id": "job:" + row["id"], "source": "job", "kind": row["kind"], "label": row["title"],
                    "refs": dict(refs_of(row.get("data") or row.get("data_json")), jobId=row["id"]),
                    "resource": "tasks", "resourceLabel": "Background tasks", "state": row["status"],
                    "created": row["created"], "started": row["updated"] if row["status"] == "running" else None,
                    "message": row.get("message"), "cancel": {"what": "job", "label": row["title"]},
                    "progress": {"fraction": row.get("progress") or 0.0} if row["status"] == "running" else None}
            stage = (row.get("flags") or {}).get("stage")
            if stage:
                item["phase"] = stage
            out.append(item)
        return out

    def snapshot(self) -> dict:
        now = time.time()
        items = []
        for part in (self._calls, self._recordings, self._jobs):
            try:
                items.extend(part())
            except Exception as error:  # noqa: BLE001 — one source failing doesn't hide the rest
                self.log(f"[queue] {part.__name__} failed: {error!r}")
        resources = {}
        for item in items:
            resources.setdefault(item["resource"], {"id": item["resource"], "label": item["resourceLabel"], "items": []})["items"].append(item)
        for res in resources.values():
            queued = [i for i in res["items"] if i["state"] == "queued"]
            if res["id"] not in ("engine",):     # the engine list is already in recording order
                queued.sort(key=lambda i: (i.get("lane") != "interactive", i.get("created") or 0))
            running = [i for i in res["items"] if i["state"] == "running"]
            for n, i in enumerate(queued, start=1):
                i["position"] = n
                i["positionText"] = ordinal(n)
            for i in running:
                i["position"] = 0
                i["elapsed"] = round(now - i["started"], 1) if i.get("started") else None
            for i in queued:
                i["waited"] = round(now - i["created"], 1) if i.get("created") else None
            res["items"] = running + queued
            res["running"], res["queued"] = len(running), len(queued)
        self._remember_ended(items)
        with self.lock:
            recent = list(self.recent)
        return {"at": now, "resources": sorted(resources.values(), key=lambda r: r["label"]),
                "running": sum(r["running"] for r in resources.values()),
                "queued": sum(r["queued"] for r in resources.values()),
                "recent": list(reversed(recent))}

    def _remember_ended(self, items):
        now_ids = {i["id"]: i for i in items}
        with self.lock:
            gone = [old for key, old in self.seen.items() if key not in now_ids]
            self.seen = now_ids
        for old in gone:
            self.recent.append(self._ended(old))

    def _ended(self, item) -> dict:
        """How an item that left the queue ended: from the job row, the track row or the provider."""
        out = {k: item.get(k) for k in ("id", "source", "kind", "label", "refs", "resource", "resourceLabel")}
        out["ended"] = time.time()
        state, error = "done", None
        try:
            if item["source"] == "job" and self.jobs is not None:
                row = self.jobs.row(item["refs"]["jobId"]) or {}
                state = {"finished": "done", "failed": "failed", "cancelled": "cancelled"}.get(row.get("status"), "done")
                error = row.get("error")
            elif item["source"] == "recording" and self.db is not None:
                row = self.db.one("SELECT status, error FROM tracks WHERE id = ?", (item["refs"]["trackId"],)) or {}
                state = {"recorded": "done", "failed": "failed"}.get(row.get("status"), "cancelled")
                error = row.get("error") if state == "failed" else None
            elif item["source"] == "call":
                ended = self.provider.ended_call(item["id"][5:])
                if ended:
                    state = "cancelled" if ended.get("cancelled") else ("failed" if ended.get("error") else "done")
                    error = ended.get("error")
        except Exception:  # noqa: BLE001
            pass
        out["state"], out["error"] = state, error
        return out

    # MARK: cancel

    def cancel(self, item_id: str) -> dict:
        source, _, key = item_id.partition(":")
        if source == "job":
            return {"ok": self.jobs.cancel(key)}
        if source == "rec":
            self.studio.cancel(key)
            self.changed()
            return {"ok": True}
        if source == "call":
            call = next((c for c in self.provider.activity() if c["id"] == key), None)
            if not call:
                return {"ok": False, "error": "That request already finished."}
            cancel = (call.get("owner") or {}).get("cancel") or {}
            if cancel.get("what") == "job" and cancel.get("id"):
                return {"ok": self.jobs.cancel(cancel["id"])}
            if cancel.get("what") == "recording" and cancel.get("id"):
                self.studio.cancel(cancel["id"])
                return {"ok": True}
            return {"ok": False, "error": "This request can't be cancelled on its own."}
        return {"ok": False, "error": "Unknown queue item."}

    # MARK: events

    def changed(self, *_):
        """Something in a queue moved: send a fresh snapshot (at most ~3 per second)."""
        with self.lock:
            if self._timer is not None:
                return
            wait = max(0.0, 0.3 - (time.time() - self._last_emit))
            self._timer = threading.Timer(wait, self._emit)
            self._timer.daemon = True
            self._timer.start()

    def _emit(self):
        with self.lock:
            self._timer = None
            self._last_emit = time.time()
        try:
            self.events.emit("queue", queue=self.snapshot())
        except Exception as error:  # noqa: BLE001
            self.log(f"[queue] couldn't send an update: {error!r}")
