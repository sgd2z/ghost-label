"""Native-library safety for the engine process, done before torch is imported.

On Windows with the ROCm torch wheels, importing scipy.linalg *after* torch can hang forever in
the DLL loader (seen with py-spy: ExtensionFileLoader.create_module in scipy/linalg/blas.py,
reached from ACE-Step's imports). scipy's bundled OpenBLAS starts its thread pool while the DLL
loads; with torch's OpenMP/ROCm DLLs already in the process that can deadlock. So we:
  * cap OpenBLAS to one thread (CPU BLAS is barely used; the GPU does the work), and
  * import scipy's compiled modules first, logging each one with its time.
Every step is logged to stderr, so a hang names the module it is stuck in."""

import faulthandler
import os
import sys
import time

# Defaults only: a value set by the user or core wins.
WINDOWS_ENV = {
    "OPENBLAS_NUM_THREADS": "1",
    "KMP_DUPLICATE_LIB_OK": "TRUE",
}

PRELOAD = ["numpy", "scipy", "scipy.linalg", "scipy.signal", "scipy.optimize"]


def log(text):
    print(f"[native] {text}", file=sys.stderr, flush=True)


def apply_env():
    if sys.platform == "win32":
        for key, value in WINDOWS_ENV.items():
            os.environ.setdefault(key, value)


def preload(log=log):
    """Import scipy's native modules before torch. Safe to call twice; skipped if torch is in."""
    apply_env()
    if "torch" in sys.modules:
        log("torch already imported; skipping the scipy-first preload")
        return
    if os.environ.get("GHOSTLABEL_NO_PRELOAD") == "1":
        return
    for name in PRELOAD:
        if name in sys.modules:
            continue
        start = time.time()
        log(f"importing {name} ...")
        try:
            __import__(name)
        except Exception as error:  # noqa: BLE001 — ACE-Step will report a real missing dep
            log(f"importing {name} failed: {error!r}")
            continue
        log(f"imported {name} in {time.time() - start:.1f} s")


class Watchdog:
    """Stack dumps only for a real stall: when `touch()` hasn't been called for `seconds`
    (no load step, no progress, no counter change), every thread's stack goes to a file
    (GHOSTLABEL_STALL_LOG, which core points at Logs/engine-stall.log) and one line to stderr.
    Normal long steps never print anything. Repeats every `again` seconds while still stalled."""

    def __init__(self, seconds=120, again=300, what="engine"):
        import threading
        self.seconds, self.again, self.what = seconds, again, what
        self.last = time.time()
        self.next_dump = self.last + seconds
        self.done = threading.Event()

    def touch(self):
        self.last = time.time()
        self.next_dump = self.last + self.seconds

    def _dump(self):
        quiet = time.time() - self.last
        path = os.environ.get("GHOSTLABEL_STALL_LOG")
        try:
            if path:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {self.what}: no progress for {quiet:.0f} s =====\n")
                    f.flush()
                    faulthandler.dump_traceback(file=f, all_threads=True)
                log(f"{self.what}: no progress for {quiet:.0f} s; stack dump written to {path}")
            else:
                log(f"{self.what}: no progress for {quiet:.0f} s; stack dump follows")
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        except Exception as error:  # noqa: BLE001
            log(f"stack dump failed: {error!r}")

    def _run(self):
        while not self.done.wait(1.0):
            if time.time() >= self.next_dump:
                self._dump()
                self.next_dump = time.time() + self.again

    def __enter__(self):
        import threading
        self.touch()
        threading.Thread(target=self._run, daemon=True, name="stall-watch").start()
        return self

    def __exit__(self, *exc):
        self.done.set()
        return False


def step(log, text):
    """Log a load step with a timestamp; returns a function that logs its end time."""
    start = time.time()
    log(f"{text} ...")

    def done(extra=""):
        log(f"{text}: done in {time.time() - start:.1f} s{(' ' + extra) if extra else ''}")
    return done
