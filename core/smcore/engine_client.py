"""Drives one engine process over the custom engine protocol (JSON lines on stdin/stdout), the way
the Mac app's EngineClient does: launch, `load`, one `generate` at a time, `cancel`, plus the
optional `analyze` extension. Restarts the engine when it dies; unloads it after idle time."""

from __future__ import annotations

import collections
import json
import os
import shlex
import subprocess
import sys
import threading
import time

from smcore.engine_layout import EngineLayout


class EngineError(Exception):
    pass


class EngineCancelled(Exception):
    pass


class EngineClient:
    def __init__(self, settings, paths, engine_dir, events, log=print, backend=None, event="engine"):
        self.settings, self.paths, self.engine_dir, self.events, self.log = settings, paths, engine_dir, events, log
        # `backend`: always this built-in backend, whatever settings select (the built-in ACE-Step
        # that songs needing reference audio go to while Lemonade is the selected engine).
        self.fixed_backend, self.event = backend, event
        self.proc = None
        self.lock = threading.RLock()        # one command conversation at a time
        self.cond = threading.Condition()
        self.inbox = []
        self.loading = None
        self.stderr_tail = collections.deque(maxlen=30)
        self.state = "stopped"
        self.error = None
        self.capabilities = {}
        self.last_used = time.time()
        self.stderr_file = None
        threading.Thread(target=self._idle_loop, daemon=True, name="engine-idle").start()

    # MARK: process

    def backend(self, eng=None):
        eng = eng or self.settings.section("engine")
        return self.fixed_backend or eng.get("backend") or "acestep"

    def command(self):
        eng = self.settings.section("engine")
        if not self.fixed_backend and eng.get("kind") == "custom" and eng.get("customCommand"):
            cmd = shlex.split(eng["customCommand"], posix=os.name != "nt")
            if eng.get("customArgs"):
                cmd += shlex.split(eng["customArgs"], posix=os.name != "nt")
            if cmd and cmd[0].endswith(".py"):
                cmd = [eng.get("python") or sys.executable] + cmd
            return cmd, os.path.dirname(cmd[0]) if os.path.isabs(cmd[0]) else None
        python = self.engine_python(eng)
        script = os.path.join(self.engine_dir, "engine.py")
        cmd = [python, script, "--backend", self.backend(eng)]
        if not self.fixed_backend:
            for key, value in (eng.get("options") or {}).items():
                cmd += ["--set", f"{key}={value}"]
        return cmd, self.engine_dir

    def layout(self):
        return EngineLayout(self.paths.envs)

    def engine_python(self, eng=None):
        """The built-in engine always runs on the installer's venv when it exists. settings.json's
        `python` is only a fallback (it can be empty or stale); core's own Python is used only for
        the mock and Lemonade backends, which need no model installed (Lemonade: just numpy)."""
        eng = eng or self.settings.section("engine")
        venv = self.layout().python()
        if venv:
            saved = eng.get("python") or ""
            if saved and os.path.normcase(os.path.abspath(saved)) != os.path.normcase(os.path.abspath(venv)):
                self.log(f"[engine] settings say python={saved}; using the engine venv {venv}")
            return venv
        saved = eng.get("python") or ""
        if saved and os.path.isfile(saved):
            return saved
        if self.backend(eng) in ("mock", "lemonade"):
            return sys.executable
        raise EngineError("The music engine isn't set up yet (no engine Python in "
                          f"{self.layout().venv}). Open Settings › Music engine and press Install engine.")

    def environment(self):
        env = dict(os.environ)
        eng = self.settings.section("engine")
        layout = self.layout()
        # The install's own environment first (ACE-Step root, ROCm variables), then any non-empty
        # values from settings. A missing or wrong ACESTEP_PROJECT_ROOT falls back to the install's.
        env.update(layout.default_env(eng.get("profile")))
        env.update({k: str(v) for k, v in (eng.get("env") or {}).items() if str(v) != ""})
        if not layout.is_ace_root(env.get("ACESTEP_PROJECT_ROOT")) and layout.ace_ok():
            env["ACESTEP_PROJECT_ROOT"] = os.path.abspath(layout.ace)
        if sys.platform == "win32":
            # scipy's OpenBLAS can deadlock in the Windows DLL loader next to the ROCm torch DLLs;
            # one BLAS thread avoids it (the GPU does the real work). User values win.
            env.setdefault("OPENBLAS_NUM_THREADS", "1")
            env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        if str(eng.get("profile") or "").startswith("rocm"):
            # Keep MIOpen's tuned-kernel database and compiled-kernel cache in our folder, so the
            # first song's kernel search/compile (VAE decode convolutions) isn't repeated.
            miopen = os.path.join(layout.root, "miopen")
            for key, sub in (("MIOPEN_USER_DB_PATH", "db"), ("MIOPEN_CUSTOM_CACHE_DIR", "cache")):
                if not env.get(key):
                    os.makedirs(os.path.join(miopen, sub), exist_ok=True)
                    env[key] = os.path.join(miopen, sub)
        resolve = getattr(self, "ffmpeg", None)
        found = resolve() if callable(resolve) else None
        if found:
            env["MUSIC_ENGINE_FFMPEG"] = found   # the same ffmpeg the startup check found or installed
        else:
            own = os.path.join(layout.root, "tools", "ffmpeg", "bin", "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            if not os.path.isfile(env.get("MUSIC_ENGINE_FFMPEG") or "") and os.path.isfile(own):
                env["MUSIC_ENGINE_FFMPEG"] = own   # a stale or lost setting falls back to our own copy
        if self.backend(eng) == "lemonade":
            from smcore import lemonade
            url, key, _ = lemonade.server(self.settings, getattr(self, "secrets", None))
            env.update({"LEMONADE_URL": url, "LEMONADE_MUSIC_MODEL": lemonade.model(self.settings)})
            if key:
                env["LEMONADE_API_KEY"] = key
        env["GHOSTLABEL_ENGINE_ROOT"] = layout.root
        env["PYTHONUNBUFFERED"] = "1"
        # UTF-8 everywhere (Hindi/Urdu lyrics in logs): stdio and open() default, on Windows too.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["GHOSTLABEL_STALL_LOG"] = os.path.join(self.paths.logs, "engine-stall.log")
        token = self.secrets.get("hfToken") if getattr(self, "secrets", None) else ""
        if token and not env.get("HF_TOKEN"):
            env["HF_TOKEN"] = token   # optional Hugging Face token (Settings › Music engine)
        return env

    def set_state(self, state, error=None):
        self.state, self.error = state, error
        self.events.emit(self.event, state=state, error=error, capabilities=self.capabilities)

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            try:
                cmd, cwd = self.command()
            except EngineError as error:
                self.set_state("failed", str(error))
                raise
            env = self.environment()
            self.log("[engine] launching: " + " ".join(cmd) + f"  (cwd {cwd}; ACESTEP_PROJECT_ROOT="
                     f"{env.get('ACESTEP_PROJECT_ROOT') or '(unset)'})")
            self.stderr_file = open(os.path.join(self.paths.logs, "engine.log"), "ab", buffering=0)
            self.stderr_file.write(f"\n==== {time.ctime()} {' '.join(cmd)}\n".encode())
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                self.proc = subprocess.Popen(cmd, cwd=cwd or None, env=env, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
            except OSError as error:
                self.set_state("failed", f"Couldn't start the engine: {error}")
                raise EngineError(self.error) from None
            with self.cond:
                self.inbox = []
            self.set_state("starting")
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True, name="engine-reader").start()
            self.stderr_tail = collections.deque(maxlen=30)
            threading.Thread(target=self._stderr_reader, args=(self.proc, self.stderr_file), daemon=True,
                             name="engine-stderr").start()

    def _stderr_reader(self, proc, log_file):
        """The engine's stderr (model loading, ACE-Step's own logs, progress bars) goes to
        Logs/engine.log and to the core's log/terminal. Progress-bar redraws ('\r') are split
        into lines and repeated bars are logged at most every 2 s."""
        buf = b""
        last_bar = [0.0, ""]

        def emit(raw):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line.strip():
                return
            self.stderr_tail.append(line[:300])
            try:
                log_file.write((line + "\n").encode("utf-8"))
            except (OSError, ValueError):
                pass
            bar = "%|" in line or "it/s]" in line or "s/it]" in line
            if bar:
                now = time.time()
                if now - last_bar[0] < 2.0 and not line.rstrip().endswith("100%"):
                    return
                last_bar[0] = now
            self.log("[engine] " + line[:400])

        stream = proc.stderr
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(1)
            if not chunk:
                break
            buf += chunk
            while True:
                cut = min([i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0], default=-1)
                if cut < 0:
                    break
                emit(buf[:cut])
                buf = buf[cut + 1:]
            if len(buf) > 8192:
                emit(buf)
                buf = b""
        if buf:
            emit(buf)

    def _reader(self, proc):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.log("[engine] non-JSON line: " + line[:200])
                continue
            if "progress" in message:
                self.events.emit("engine_progress", progress=message["progress"])
            with self.cond:
                self.inbox.append(message)
                self.cond.notify_all()
        code = proc.wait()
        time.sleep(0.2)  # let the stderr thread catch the last lines
        with self.cond:
            self.inbox.append({"_exited": code})
            self.cond.notify_all()
        if self.proc is proc:
            self.proc = None
            self.loading = None
            if self.state not in ("stopped",):
                self.set_state("failed" if code else "stopped", self.exit_message(code) if code else None)

    def exit_message(self, code):
        """'The engine exited (code N)' plus its last real stderr line (skipping progress bars)."""
        last = ""
        for line in reversed(list(getattr(self, "stderr_tail", []) or [])):
            if line.strip() and "%|" not in line and not line.startswith(("Traceback", "  File")):
                last = line.strip()
                break
        text = f"The engine exited unexpectedly (code {code})."
        if last:
            text += f" Its last message: {last[:240]}"
        return text + " Full output: Logs/engine.log."

    def send(self, message):
        if not self.proc or self.proc.poll() is not None:
            raise EngineError("The engine isn't running.")
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def wait_for(self, predicate, timeout, on_message=None):
        deadline = time.time() + timeout
        with self.cond:
            while True:
                while self.inbox:
                    message = self.inbox.pop(0)
                    if "_exited" in message:
                        raise EngineError(self.exit_message(message["_exited"]))
                    if on_message:
                        on_message(message)
                    if predicate(message):
                        return message
                left = deadline - time.time()
                if left <= 0:
                    raise EngineError("The engine stopped responding.")
                self.cond.wait(min(left, 1.0))

    def stop(self):
        with self.lock:
            proc = self.proc
            self.proc = None
            self.set_state("stopped")
            if proc and proc.poll() is None:
                try:
                    self.send_to(proc, {"shutdown": {}})
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    proc.kill()

    @staticmethod
    def send_to(proc, message):
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()

    def _idle_loop(self):
        while True:
            time.sleep(15)
            minutes = float(self.settings.section("engine").get("idleUnloadMinutes") or 0)
            if self.fixed_backend and self.proc and not minutes:
                minutes = 10   # the stand-in engine never stays loaded for good next to Lemonade
            if minutes > 0 and self.state == "ready" and time.time() - self.last_used > minutes * 60:
                if self.lock.acquire(blocking=False):
                    try:
                        self.log("[engine] idle; unloading")
                        self.stop()
                    finally:
                        self.lock.release()

    # MARK: commands

    # A host that sends `loading` heartbeats (ours: every 2 s) and then goes quiet this long is stuck.
    LOAD_SILENCE = 90

    def ensure_ready(self, timeout=1800, on_loading=None):
        with self.lock:
            if self.state == "ready" and self.proc and self.proc.poll() is None:
                return self.capabilities
            self.start()
            self.set_state("loading")
            self.send({"load": {"modelPath": self.settings.section("engine").get("modelPath") or self.engine_dir}})

            def is_answer(m):
                return "ready" in m or ("failed" in m and not (m["failed"] or {}).get("id"))

            started = time.time()
            self.log("[engine] loading the model into memory (the first load can take several minutes)")
            stop_beat = threading.Event()

            def beat():
                while not stop_beat.wait(30):
                    self.log(f"[engine] still loading the model ({int(time.time() - started)} s)")
            threading.Thread(target=beat, daemon=True, name="engine-load-beat").start()
            deadline = started + timeout
            self.loading = {"stage": "Starting the engine", "elapsed": 0.0}
            heard = {"beat": False, "stage": "Starting the engine"}

            def on_message(m):
                if "loading" in m:
                    info = m["loading"] or {}
                    heard.update(beat=True, stage=info.get("stage") or heard["stage"])
                    self.loading = {"stage": heard["stage"], "elapsed": float(info.get("elapsed") or 0)}
                    self.events.emit(self.event + "_loading", loading=self.loading)
                    if on_loading:
                        on_loading(self.loading)
            try:
                while True:
                    left = deadline - time.time()
                    if left <= 0:
                        raise EngineError(f"Loading the model took over {timeout // 60} min "
                                          f"(last step: {heard['stage']}). See Logs/engine.log.")
                    wait = min(left, self.LOAD_SILENCE) if heard["beat"] else left
                    try:
                        message = self.wait_for(lambda m: is_answer(m) or "loading" in m, wait, on_message)
                    except EngineError as error:
                        if "stopped responding" not in str(error):
                            raise
                        if time.time() < deadline:
                            raise EngineError(
                                f"The engine stopped responding while loading the model (no heartbeat for "
                                f"{self.LOAD_SILENCE} s; last step: {heard['stage']}). Its stack dump is in "
                                "Logs/engine.log.") from None
                        continue
                    if is_answer(message):
                        break
            except EngineError as error:
                self.loading = None
                self.stop_quietly()
                self.set_state("failed", str(error))
                raise
            finally:
                stop_beat.set()
                self.loading = None
            if "ready" in message:
                self.log(f"[engine] model loaded in {int(time.time() - started)} s")
            if "failed" in message:
                self.set_state("failed", (message["failed"] or {}).get("message"))
                self.stop_quietly()
                raise EngineError(self.error)
            self.capabilities = (message["ready"] or {}).get("capabilities") or {}
            self.set_state("ready")
            self.last_used = time.time()
            return self.capabilities

    def stop_quietly(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.kill()

    def generate(self, job: dict, on_progress=None, timeout_silence=180, on_loading=None):
        with self.lock:
            self.ensure_ready(on_loading=on_loading)
            self.set_state("busy")
            jid = job["id"]
            try:
                self.send({"generate": {"_0": job}})

                seen = {"stage": None, "logged": 0.0}

                def handle(m):
                    if "progress" in m and (m["progress"] or {}).get("id") == jid:
                        p = m["progress"].get("_1") or {}
                        now = time.time()
                        if p.get("stage") != seen["stage"] or now - seen["logged"] >= 15:
                            seen.update(stage=p.get("stage"), logged=now)
                            counts = (f"step {p['step']}/{p['steps']}" if p.get("step") else
                                      f"chunk {p['chunk']}/{p['chunks']}" if p.get("chunk") else
                                      f"{p['tokens']} tokens" if p.get("tokens") else
                                      ("estimated " if p.get("estimated") else "") +
                                      f"{round(100 * float(p.get('stageFraction') or 0))}% of stage")
                            self.log(f"[engine] recording {jid}: {p.get('detail') or p.get('stage')} · {counts} · "
                                     f"{int(float(p.get('elapsed') or 0))} s")
                        if on_progress:
                            on_progress(p)

                def done(m):
                    if "finished" in m:
                        return (m["finished"].get("_0") or {}).get("id") == jid
                    if "failed" in m:
                        return (m["failed"] or {}).get("id") in (jid, None)
                    if "cancelled" in m:
                        return (m["cancelled"] or {}).get("id") == jid
                    return False

                # Heartbeats arrive every second, so a long silence means a stuck engine.
                while True:
                    try:
                        message = self.wait_for(lambda m: done(m) or "progress" in m, timeout_silence, handle)
                    except EngineError as error:
                        if "stopped responding" in str(error):
                            error = EngineError(f"The engine stopped responding while recording (no progress for "
                                                f"{timeout_silence // 60} min; last stage: {seen['stage'] or 'starting'}). "
                                                "Its stack dump is in Logs/engine.log.")
                        self.stop_quietly()
                        self.set_state("failed", str(error))
                        raise error from None
                    if done(message):
                        break
                if "finished" in message:
                    return message["finished"]["_0"]
                if "cancelled" in message:
                    raise EngineCancelled()
                raise EngineError((message["failed"] or {}).get("message") or "The engine failed.")
            finally:
                self.last_used = time.time()
                if self.state == "busy":
                    self.set_state("ready")

    def cancel(self, jid):
        try:
            self.send({"cancel": {"id": jid}})
        except EngineError:
            pass

    def analyze(self, path, timeout=900):
        with self.lock:
            caps = self.ensure_ready()
            if not caps.get("analyze"):
                raise EngineError("This engine can't analyze audio (it needs the built-in ACE-Step's LM planner).")
            self.set_state("busy")
            jid = f"an_{int(time.time() * 1000)}"
            try:
                self.send({"analyze": {"_0": {"id": jid, "audioPath": path}}})
                message = self.wait_for(lambda m: ("analyzed" in m and m["analyzed"]["_0"].get("id") == jid)
                                        or ("failed" in m and (m["failed"] or {}).get("id") in (jid, None)), timeout)
            finally:
                self.last_used = time.time()
                if self.state == "busy":
                    self.set_state("ready")
            if "failed" in message:
                raise EngineError(message["failed"].get("message") or "Analysis failed")
            return message["analyzed"]["_0"].get("result") or {}

    def status(self):
        try:
            command = " ".join(self.command()[0])
        except EngineError as error:
            command = f"(not runnable: {error})"
        return {"state": self.state, "error": self.error, "capabilities": self.capabilities, "command": command,
                "loading": self.loading}
