"""Background jobs (library builds, link analysis, installs) with progress, cancel and events."""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from smcore.db import new_id


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, manager, row):
        self.manager = manager
        self.id = row["id"]
        self.kind = row["kind"]
        self.cancel_event = threading.Event()
        self.key = row.get("key")

    def check(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def progress(self, fraction: float, message: str = None):
        self.check()
        self.manager.update(self.id, progress=max(0.0, min(1.0, float(fraction))),
                            **({"message": message} if message is not None else {}))

    def log(self, message: str):
        self.manager.log(f"[job {self.kind} {self.id}] {message}")
        self.manager.update(self.id, message=message)

    def tail(self, line: str):
        """Raw output lines shown under the job card (kept in memory, last 40)."""
        self.manager.add_tail(self.id, line)

    def set_flags(self, **flags):
        """Transient UI state such as {"stalled": 95}; sent with the job, not stored."""
        self.manager.set_flags(self.id, **flags)

    def stage(self, name: str):
        """The step the job is on (the UI lists the steps and marks this one active)."""
        self.manager.set_flags(self.id, stage=name)

    def count(self, **delta):
        """Add to named counters shown by the UI, e.g. count(albumsWritten=1)."""
        self.manager.bump(self.id, **delta)


class JobManager:
    def __init__(self, db, events, log=print, workers: int = 4):
        self.db = db
        self.events = events
        self.log = log
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self.running = {}
        self.singles = {}     # single-flight key -> job id
        self.tails = {}       # job id -> recent output lines
        self.flags = {}       # job id -> transient flags
        self.lock = threading.RLock()
        self.on_cancel = []   # callbacks(job_id): e.g. the provider drops the job's queued AI requests
        # Cards left over from earlier sessions are cleared; jobs that were running when the app
        # quit can't resume in-process, so they're marked (the UI offers Retry / dismiss).
        db.run("DELETE FROM jobs WHERE status = 'interrupted'")
        db.run("UPDATE jobs SET status = 'interrupted', updated = ? WHERE status IN ('queued', 'running')",
               (time.time(),))

    def row(self, job_id):
        return self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def decorate(self, row):
        if row:
            row["tail"] = list(self.tails.get(row["id"], []))
            row["flags"] = dict(self.flags.get(row["id"], {}))
        return row

    def list(self, limit=50):
        return [self.decorate(r) for r in self.db.all("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))]

    def update(self, job_id, **values):
        values["updated"] = time.time()
        self.db.update("jobs", job_id, values)
        row = self.decorate(self.row(job_id))
        self.events.emit("job", job=row)
        return row

    def add_tail(self, job_id, line):
        with self.lock:
            tail = self.tails.setdefault(job_id, [])
            tail.append(line[:300])
            del tail[:-40]

    def set_flags(self, job_id, **flags):
        with self.lock:
            current = self.flags.setdefault(job_id, {})
            for k, v in flags.items():
                if v is None:
                    current.pop(k, None)
                else:
                    current[k] = v
        self.events.emit("job", job=self.decorate(self.row(job_id)))

    def bump(self, job_id, **delta):
        with self.lock:
            counts = self.flags.setdefault(job_id, {}).setdefault("counts", {})
            for k, v in delta.items():
                counts[k] = counts.get(k, 0) + v
            self.flags[job_id]["counts"] = dict(counts)
        self.events.emit("job", job=self.decorate(self.row(job_id)))

    def active(self, key):
        """The running job for a single-flight key, if any."""
        with self.lock:
            job_id = self.singles.get(key)
            return self.running.get(job_id) if job_id else None

    def dismiss(self, job_id) -> bool:
        with self.lock:
            if job_id in self.running:
                return False
            self.tails.pop(job_id, None)
            self.flags.pop(job_id, None)
        self.db.run("DELETE FROM jobs WHERE id = ?", (job_id,))
        self.events.emit("job", job={"id": job_id, "status": "dismissed"})
        return True

    def start(self, kind: str, title: str, fn, data=None, key: str = None) -> dict:
        """Start fn(job) in the pool. With a key, the job is single-flight: while one with the same
        key is queued or running, callers get that job back (attached=True) instead of a new one."""
        with self.lock:
            if key:
                existing = self.active(key)
                if existing:
                    row = self.decorate(self.row(existing.id))
                    row["attached"] = True
                    return row
            now = time.time()
            row = {"id": new_id("job_"), "kind": kind, "title": title, "status": "queued", "progress": 0.0,
                   "data_json": data or {}, "created": now, "updated": now}
            self.db.insert("jobs", row)
            job = Job(self, {"id": row["id"], "kind": kind, "key": key})
            self.running[job.id] = job
            if key:
                self.singles[key] = job.id

        def run():
            from smcore.queue import owner, refs_of
            final = {"status": "failed", "error": "stopped"}
            try:
                job.check()     # cancelled while it waited for a worker
                self.update(job.id, status="running")
                with owner(jobId=job.id, kind=kind, label=title, cancel={"what": "job", "id": job.id, "label": title},
                           **refs_of(data)):
                    result = fn(job)
                final = {"status": "finished", "progress": 1.0, "result_json": result or {}}
            except Cancelled:
                final = {"status": "cancelled"}
            except Exception as error:  # noqa: BLE001
                self.log(traceback.format_exc())
                final = {"status": "failed", "error": str(error) or repr(error)}
            finally:
                # Leave the running set (and free the single-flight key) before the final status is
                # visible, so whoever sees "finished" can start the same job again at once.
                with self.lock:
                    self.running.pop(job.id, None)
                    if key and self.singles.get(key) == job.id:
                        self.singles.pop(key, None)
                    last = self.flags.pop(job.id, None) or {}
                final["updated"] = time.time()
                self.db.update("jobs", job.id, final)
                row = self.decorate(self.row(job.id))
                if row is not None:
                    row["flags"] = {k: v for k, v in last.items() if k in ("stage", "counts")}
                self.events.emit("job", job=row)

        self.pool.submit(run)
        return self.decorate(self.row(job.id))

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.running.get(job_id)
        if job:
            job.cancel_event.set()
            for fn in self.on_cancel:
                try:
                    fn(job_id)
                except Exception:  # noqa: BLE001
                    pass
            return True
        return False
