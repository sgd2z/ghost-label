"""Running installer child processes: whole-tree kill (Windows job object + taskkill /T /F, POSIX
process groups), cancel that works even while the child is silent, byte progress parsed from
pip's `--progress-bar raw` lines, and a stall watchdog. All mechanical; no judgment here."""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import threading
import time

from smcore.jobs import Cancelled

IS_WIN = os.name == "nt"
DOWNLOADING = re.compile(r"Downloading (\S+?)(?: \(([\d.]+ [kMG]?B)\))?$")
RAW_PROGRESS = re.compile(r"^Progress (\d+) of (\d+)")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")


class Stalled(RuntimeError):
    pass


# MARK: Windows job object (children die with the job handle, even if the core is killed)

_job_handle = None


def _win_job():
    global _job_handle
    if _job_handle is not None or not IS_WIN:
        return _job_handle
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))  # ExtendedLimitInformation
        _job_handle = (k32, handle)
    except Exception:  # noqa: BLE001 — fall back to taskkill only
        _job_handle = False
    return _job_handle


def _assign(proc):
    job = _win_job()
    if not job:
        return
    try:
        import ctypes
        k32, handle = job
        PROCESS_ALL_ACCESS = 0x1F0FFF
        h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
        if h:
            k32.AssignProcessToJobObject(handle, h)
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass


def spawn(args, cwd=None, env=None):
    kwargs = {}
    if IS_WIN:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([str(a) for a in args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    if IS_WIN:
        _assign(proc)
    return proc


def kill_tree(proc, wait=15):
    """Kill the process and everything it started."""
    if proc.poll() is None:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=wait)


def dir_bytes(paths):
    total = 0
    for root in paths or []:
        for base, _dirs, files in os.walk(root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
    return total


def _reader(stream, out):
    """Split on \\r as well as \\n: progress bars rewrite one line with \\r."""
    buf = b""
    while True:
        chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            if p:
                out.put(p)
    if buf:
        out.put(buf)
    out.put(None)


def run_watched(args, cwd=None, env=None, job=None, log_path=None, say=print, on_progress=None,
                stall_warn=90, stall_kill=600, watch_dirs=None, tick=0.5):
    """Run a child; returns (exit code, last lines). Raises Cancelled or Stalled after killing the tree.

    Activity = a new output line, a `Progress N of M` advance, or bytes growing under watch_dirs.
    No activity for stall_warn s sets job flag `stalled`; for stall_kill s kills and raises Stalled."""
    proc = spawn(args, cwd, env)
    lines = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, lines), daemon=True).start()
    tail, current, last_bytes = [], {"name": None, "size": None}, dir_bytes(watch_dirs)
    last_activity = last_report = last_dir_check = time.time()
    rate_mark = (time.time(), 0)
    warned = False
    logf = open(log_path, "ab") if log_path else None
    try:
        eof = False
        while not eof:
            try:
                raw = lines.get(timeout=tick)
            except queue.Empty:
                raw = b""
            now = time.time()
            if raw is None:
                eof = True
            elif raw:
                line = ANSI.sub("", raw.decode("utf-8", "replace")).rstrip()
                raw = line.encode("utf-8")
                if not line.strip():
                    continue
                m = RAW_PROGRESS.match(line)
                if m:
                    done, total = int(m.group(1)), int(m.group(2))
                    if done > rate_mark[1] or done == 0:
                        last_activity = now
                    if now - last_report >= 1 or done == total:
                        rate = (done - rate_mark[1]) / max(1e-6, now - rate_mark[0])
                        rate_mark = (now, done)
                        last_report = now
                        if on_progress:
                            on_progress(current["name"], done, total, rate)
                else:
                    last_activity = now
                    if logf:
                        logf.write(raw + b"\n")
                    tail = (tail + [line])[-30:]
                    if job:
                        job.tail(line)
                    d = DOWNLOADING.search(line.strip())
                    if d:
                        current = {"name": d.group(1).rsplit("/", 1)[-1], "size": d.group(2)}
                        rate_mark = (now, 0)
                        say(line.strip()[:200])
                    elif "Successfully" in line or "error" in line.lower() or line.startswith("Collecting"):
                        say(line.strip()[:200])
            if watch_dirs and now - last_dir_check >= 2:
                last_dir_check = now
                b = dir_bytes(watch_dirs)
                if b != last_bytes:
                    last_bytes, last_activity = b, now
            if job and job.cancel_event.is_set():
                kill_tree(proc)
                raise Cancelled()
            idle = now - last_activity
            if stall_warn and idle >= stall_warn:
                if job and (not warned or int(idle) % 10 == 0):
                    job.set_flags(stalled=int(idle))
                    job.log(f"No progress for {int(idle)} s" + (f" (downloading {current['name']})" if current["name"] else ""))
                warned = True
            elif warned:
                warned = False
                if job:
                    job.set_flags(stalled=None)
            if stall_kill and idle >= stall_kill:
                kill_tree(proc)
                raise Stalled(f"No progress for {int(idle)} s; stopped the step"
                              + (f" while downloading {current['name']}" if current["name"] else "") + ".")
        code = proc.wait()
        return code, tail
    finally:
        if proc.poll() is None:
            kill_tree(proc)
        if logf:
            logf.close()
        if job and warned:
            job.set_flags(stalled=None)


# MARK: one core per data folder

_core_lock = None


def _kill_pid_tree(pid):
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        for target in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
            try:
                target()
            except (ProcessLookupError, PermissionError, OSError):
                pass


def hold_core_lock(home, log=print, wait=15):
    """Take <home>/core.lock for this process's lifetime. If an earlier core still holds it (left
    running when the app quit), stop that core and its children first: two cores on one data
    folder each keep their own copy of the settings and in-flight installs."""
    global _core_lock
    path = os.path.join(home, "core.lock")
    f = open(path, "a+b")

    def try_lock():
        try:
            if IS_WIN:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    if not try_lock():
        try:
            with open(path + ".pid", encoding="ascii") as p:
                old = int(p.read().strip() or 0)
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid():
            log(f"another core (pid {old}) is still using {home}; stopping it and its child processes")
            _kill_pid_tree(old)
        deadline = time.time() + wait
        while not try_lock():
            if time.time() > deadline:
                log("couldn't take the data-folder lock; continuing anyway")
                break
            time.sleep(0.2)
    with open(path + ".pid", "w", encoding="ascii") as p:
        p.write(str(os.getpid()))
    _core_lock = f
    return f


def kill_stale_children(dirs, log=print):
    """Windows: stop processes running a Python from our engine folders (orphaned pip/uv or engine
    processes from an earlier session whose core is gone). Runs before this core starts any."""
    if not IS_WIN or not dirs:
        return 0
    cond = " -or ".join("$_.ExecutablePath -like '" + d.replace("'", "''").rstrip("\\") + "\\*' -or $_.CommandLine -like '*"
                        + d.replace("'", "''").rstrip("\\") + "\\*'" for d in dirs)
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.ProcessId -ne " + str(os.getpid()) + " -and $_.ProcessId -ne " + str(os.getppid()) + " -and $_.Name -notlike 'powershell*' -and (" + cond + ") } | "
              "ForEach-Object { $_.ProcessId; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        pids = [x for x in r.stdout.split() if x.isdigit() and int(x) != os.getpid()]
        if pids:
            log(f"stopped {len(pids)} leftover process(es) from an earlier session: {', '.join(pids)}")
        return len(pids)
    except Exception as error:  # noqa: BLE001
        log(f"couldn't check for leftover processes: {error}")
        return 0
