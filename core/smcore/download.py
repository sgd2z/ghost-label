"""Resumable HTTP downloads: a `.partial` file plus HTTP Range, retries with backoff, a read
timeout, size/sha256 checks, and progress callbacks. Mechanical; used for big installer files."""

from __future__ import annotations

import hashlib
import os
import time
import urllib.error
import urllib.request

from smcore.jobs import Cancelled
from smcore.net import USER_AGENT, _context


def fetch(url, dest, on_progress=None, cancelled=None, sha256=None, expected_size=None, retries=8,
          timeout=60, chunk=1 << 20, headers=None):
    """Download url to dest, resuming dest + '.partial' if present. Returns dest."""
    if os.path.exists(dest) and (expected_size is None or os.path.getsize(dest) == expected_size):
        return dest
    part = dest + ".partial"
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    if expected_size is not None and os.path.exists(part) and os.path.getsize(part) > expected_size:
        os.remove(part)
    attempt = 0
    while True:
        have = os.path.getsize(part) if os.path.exists(part) else 0
        hdrs = dict(headers or {}, **{"User-Agent": USER_AGENT})
        if have:
            hdrs["Range"] = f"bytes={have}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=timeout,
                                        context=_context() if url.startswith("https") else None) as r:
                status = getattr(r, "status", 200)
                if have and status != 206:
                    have = 0          # server ignored Range: start over
                length = r.headers.get("Content-Length")
                total = (have + int(length)) if length else expected_size
                mark, mark_bytes = time.time(), have
                with open(part, "ab" if have else "wb") as f:
                    done = have
                    while True:
                        if cancelled and cancelled():
                            raise Cancelled()
                        data = r.read(chunk)
                        if not data:
                            break
                        f.write(data)
                        done += len(data)
                        now = time.time()
                        if on_progress and (now - mark >= 1):
                            on_progress(done, total, (done - mark_bytes) / (now - mark))
                            mark, mark_bytes = now, done
                if total and done < total:
                    raise IOError(f"connection closed at {done} of {total} bytes")
                if on_progress:
                    on_progress(done, total, 0)
            break
        except urllib.error.HTTPError as error:
            if error.code == 416 and have:   # already complete
                break
            attempt += 1
            if attempt > retries or 400 <= error.code < 500:
                raise
        except (OSError, IOError) as error:
            attempt += 1
            if attempt > retries:
                raise IOError(f"download failed after {retries} retries: {error}") from error
        time.sleep(min(30, 2 ** attempt))
    size = os.path.getsize(part)
    if expected_size is not None and size != expected_size:
        os.remove(part)
        raise IOError(f"size mismatch: got {size}, expected {expected_size}")
    if sha256:
        h = hashlib.sha256()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest().lower() != sha256.lower():
            os.remove(part)
            raise IOError("sha256 mismatch; the partial file was deleted, retry to download again")
    os.replace(part, dest)
    return dest


def _get_range(url, start, end, headers, timeout):
    h = dict(headers or {}, **{"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"})
    r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout,
                               context=_context() if url.startswith("https") else None)
    if getattr(r, "status", 200) != 206:
        r.close()
        raise IOError(f"server ignored the Range request (HTTP {getattr(r, 'status', '?')})")
    return r


def fetch_parallel(url, dest, size, on_progress=None, cancelled=None, sha256=None, connections=8,
                   piece=32 << 20, retries=10, timeout=60, headers=None):
    """Download a big file with several HTTP Range requests at once (one connection is often capped
    well below the line speed). Resumable: dest.partial plus dest.partial.json listing finished
    pieces; a plain sequential dest.partial (e.g. from fetch() or another downloader) counts as a
    finished prefix. Verifies size and sha256, then renames into place."""
    import json
    import threading
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        return dest
    if size < 2 * piece or connections <= 1:
        return fetch(url, dest, on_progress=on_progress, cancelled=cancelled, sha256=sha256,
                     expected_size=size, retries=retries, timeout=timeout, headers=headers)
    part, state_path = dest + ".partial", dest + ".partial.json"
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    count = (size + piece - 1) // piece
    done = set()
    try:
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f)
        if st.get("size") == size and st.get("piece") == piece:
            done = {int(i) for i in st.get("done", [])}
    except (OSError, ValueError):
        if os.path.exists(part):  # sequential partial: whole pieces below its length are finished
            have = min(os.path.getsize(part), size)
            done = set(range(have // piece))
    if not os.path.exists(part):
        open(part, "wb").close()
    if os.path.getsize(part) > size:
        os.truncate(part, size)
    with open(part, "r+b") as f:
        f.truncate(size)
    lock = threading.Lock()
    todo = [i for i in range(count) if i not in done]
    progress = {"bytes": sum(min(piece, size - i * piece) for i in done)}
    errors = []
    stop = threading.Event()

    def save_state():
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"size": size, "piece": piece, "done": sorted(done)}, f)
        os.replace(tmp, state_path)

    def worker():
        with open(part, "r+b") as f:
            while not stop.is_set():
                with lock:
                    if not todo:
                        return
                    i = todo.pop(0)
                start, end = i * piece, min(size, (i + 1) * piece) - 1
                pos, attempt = start, 0
                while pos <= end and not stop.is_set():
                    try:
                        with _get_range(url, pos, end, headers, timeout) as r:
                            while pos <= end:
                                if stop.is_set() or (cancelled and cancelled()):
                                    stop.set()
                                    return
                                data = r.read(min(1 << 20, end - pos + 1))
                                if not data:
                                    raise IOError("connection closed early")
                                f.seek(pos)
                                f.write(data)
                                pos += len(data)
                                with lock:
                                    progress["bytes"] += len(data)
                    except Exception as error:  # noqa: BLE001
                        attempt += 1
                        if attempt > retries:
                            errors.append(error)
                            stop.set()
                            return
                        time.sleep(min(30, 2 ** attempt))
                if pos > end:
                    f.flush()
                    with lock:
                        done.add(i)
                        save_state()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(connections, len(todo)) or 0)]
    for t in threads:
        t.start()
    mark, mark_bytes = time.time(), progress["bytes"]
    while any(t.is_alive() for t in threads):
        time.sleep(0.5)
        now = time.time()
        if on_progress and now - mark >= 1:
            b = progress["bytes"]
            on_progress(b, size, (b - mark_bytes) / (now - mark))
            mark, mark_bytes = now, b
        if cancelled and cancelled():
            stop.set()
    if cancelled and cancelled():
        raise Cancelled()
    if errors:
        raise IOError(f"download failed after {retries} retries: {errors[0]}")
    if len(done) != count:
        raise IOError("download stopped before all pieces finished")
    if on_progress:
        on_progress(size, size, 0)
    if sha256:
        h = hashlib.sha256()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest().lower() != sha256.lower():
            os.remove(part)
            os.remove(state_path)
            raise IOError("sha256 mismatch; the partial file was deleted, retry to download again")
    os.replace(part, dest)
    try:
        os.remove(state_path)
    except OSError:
        pass
    return dest
