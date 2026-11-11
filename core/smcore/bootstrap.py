"""First-run engine setup: detect the GPU, then build the engine's own Python environment.

Profiles (the listener confirms one in Settings › Engine):
- rocm-windows: AMD ROCm PyTorch wheels for gfx1151 (Ryzen AI Max+ / Strix Halo) from AMD's
  index, then ACE-Step 1.5's ROCm requirements constrained so they can't replace torch. Mirrors
  engine/docs/backends/acestep/windows-rocm.md.
- cuda: ACE-Step's own requirements.txt (CUDA torch from download.pytorch.org), NVIDIA.
- mps: ACE-Step's requirements.txt on Apple Silicon (PyPI torch with MPS, MLX for the LM).
- cpu: CPU torch, then the torch-agnostic requirement list. Slow, but works anywhere.
- mock: no model at all (test tones), to check the plumbing.
- lemonade: no model either; the small environment the engine host needs (numpy) to drive AMD
  Lemonade Server's ACE-Step-Music. On top of a full install it only selects Lemonade.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile

from smcore import download, net, procs
from smcore.engine_layout import ROCM_WINDOWS_ENV, EngineLayout
from smcore.jobs import Cancelled

AMD_INDEX = "https://stable.repo.amd.com/rocm/whl-next/"
ROCM_WHEELS = {
    "10.0.0": ["torch[device-{arch}]==2.13.0+rocm10.0.0", "torchvision[device-{arch}]==0.28.0+rocm10.0.0",
               "torchaudio==2.11.0.2+rocm10.0.0"],
    "10.1.0": ["torch[device-{arch}]==2.13.0+rocm10.1.0", "torchvision[device-{arch}]==0.28.0+rocm10.1.0",
               "torchaudio==2.11.0.2+rocm10.1.0"],
}
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
ACESTEP_MAIN_REPO = "ACE-Step/Ace-Step1.5"   # acestep.model_downloader MAIN_MODEL_REPO -> <root>/checkpoints
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
ACESTEP_ZIP = "https://codeload.github.com/ace-step/ACE-Step-1.5/zip/refs/heads/{ref}"
# Pinned portable ffmpeg for Windows (gyan.dev essentials build, via its GitHub release), checked by sha256.
FFMPEG_WIN = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
FFMPEG_WIN_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
PROFILES = ("rocm-windows", "cuda", "mps", "cpu", "lemonade", "mock")
LIGHT = ("mock", "lemonade")   # no PyTorch, no ACE-Step: only the engine host's own requirements


def fmt_eta(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def wheel_ok(path):
    """A finished wheel's zip directory reads cleanly (catches truncated files)."""
    try:
        with zipfile.ZipFile(path) as z:
            return bool(z.namelist())
    except (zipfile.BadZipFile, OSError):
        return False

GPU_CHECK = ("import torch, json; ok = torch.cuda.is_available() or (hasattr(torch.backends, 'mps') and torch.backends.mps.is_available()); "
             "dev = 'cuda' if torch.cuda.is_available() else ('mps' if ok else 'cpu'); "
             "name = torch.cuda.get_device_properties(0).name if dev == 'cuda' else dev; "
             "arch = getattr(torch.cuda.get_device_properties(0), 'gcnArchName', '') if dev == 'cuda' else ''; "
             "x = (torch.zeros(8, device=dev) + 1).sum().item(); "
             "print(json.dumps({'torch': torch.__version__, 'hip': getattr(torch.version, 'hip', None), 'device': dev, 'name': name, 'arch': arch, 'kernel': x}))")


def run_capture(args, timeout=30):
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def detect():
    """What hardware is here, and which profile fits it (the listener can override)."""
    system, machine = platform.system(), platform.machine().lower()
    gpus = []
    if system == "Windows":
        text = run_capture(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,PNPDeviceID | ConvertTo-Json"])
        try:
            data = json.loads(text) if text.strip() else []
            data = data if isinstance(data, list) else [data]
            gpus = [{"name": d.get("Name"), "vendor": d.get("AdapterCompatibility"), "id": d.get("PNPDeviceID")} for d in data]
        except ValueError:
            pass
    elif system == "Linux":
        for line in run_capture(["lspci", "-nn"]).splitlines():
            if re.search(r"VGA|3D|Display", line):
                gpus.append({"name": line.split(": ", 1)[-1], "vendor": "", "id": ""})
    elif system == "Darwin":
        gpus.append({"name": "Apple " + machine, "vendor": "Apple", "id": ""})
    nvidia = bool(shutil.which("nvidia-smi")) and bool(run_capture(["nvidia-smi", "-L"]))
    amd = any(re.search(r"AMD|Radeon|ATI|VEN_1002", " ".join(str(v) for v in g.values()), re.I) for g in gpus)
    if nvidia:
        suggested = "cuda"
    elif system == "Windows" and amd:
        suggested = "rocm-windows"
    elif system == "Darwin" and machine in ("arm64", "aarch64"):
        suggested = "mps"
    else:
        suggested = "cpu"
    return {"system": system, "machine": machine, "gpus": gpus, "nvidia": nvidia, "amd": amd,
            "suggested": suggested, "profiles": list(PROFILES), "rocmVersions": list(ROCM_WHEELS),
            "defaultArch": "gfx1151", "python": sys.version.split()[0]}


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Installer:
    """Builds the engine environment. One install at a time (JobManager single-flight plus a lock
    here). Big downloads go to a persistent wheelhouse so a retried install reuses them."""

    STALL_WARN = 90       # seconds without output/bytes before the card says "No progress for N s"
    STALL_KILL = 600      # seconds without output/bytes before the step is stopped and retried
    STEP_ATTEMPTS = 3

    def __init__(self, settings, paths, engine_dir, uv=None, log=print):
        self.settings, self.paths, self.engine_dir, self.log = settings, paths, engine_dir, log
        self.uv = uv if uv and os.path.exists(uv) else shutil.which("uv")
        self.root = paths.envs
        self.venv = os.path.join(self.root, "venv")
        self.ace = os.path.join(self.root, "ACE-Step-1.5")
        self.tools = os.path.join(self.root, "tools")
        self.tmp = os.path.join(self.root, "tmp")              # TEMP for children; cleared after each run
        self.wheels = os.path.join(self.root, "wheels")         # kept: finished wheels are reused on retry
        self.partials = os.path.join(self.wheels, "partial")    # interrupted wheel downloads, resumed by us
        self.downloads = os.path.join(self.root, "downloads")
        self.marker = os.path.join(self.venv, ".ghostlabel-ready")
        self.lock = threading.Lock()
        self.secrets = None           # set by App: optional Hugging Face token (hfToken)
        self.connections = int(os.environ.get("GL_DOWNLOAD_CONNECTIONS") or 8)  # parallel ranges per big file
        self.job = None
        self.step_span = (0.0, 1.0)

    @property
    def layout(self):
        return EngineLayout(self.root)

    @property
    def python(self):
        return self.layout.python() or self.layout.default_python()

    def say(self, text):
        self.log("[install] " + text)
        if self.job:
            self.job.log(text)

    def child_env(self, extra=None):
        e = dict(os.environ)
        os.makedirs(self.tmp, exist_ok=True)
        e.update({
            "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            # Children's temp files (pip's in-flight downloads) live here so they can be salvaged/cleaned.
            "TEMP": self.tmp, "TMP": self.tmp, "TMPDIR": self.tmp,
            # Network: longer socket timeout, more connection retries, resume broken downloads in-run.
            "PIP_TIMEOUT": "60", "PIP_RETRIES": "10", "PIP_RESUME_RETRIES": "10",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "UV_HTTP_TIMEOUT": "120", "UV_HTTP_RETRIES": "10",
            # Plain text in the log and job card (no ANSI colors / progress-bar escapes).
            "NO_COLOR": "1", "LOGURU_COLORIZE": "False", "PY_COLORS": "0", "CLICOLOR": "0", "TERM": "dumb",
            "HF_HUB_DISABLE_PROGRESS_BARS": "0", "HF_HUB_ENABLE_HF_TRANSFER": "0",
        })
        token = self.secrets.get("hfToken") if self.secrets else ""
        if token:
            e["HF_TOKEN"] = token
        e.update(extra or {})
        return e

    def on_progress(self, name, done, total, rate):
        if not self.job:
            return
        lo, hi = self.step_span
        frac = done / total if total else 0
        self.job.progress(lo + (hi - lo) * frac,
                          f"Downloading {name or 'file'}: {done / 1e6:.0f} / {total / 1e6:.0f} MB ({frac * 100:.0f}%)"
                          + (f" · {rate / 1e6:.1f} MB/s" if rate else ""))

    def run(self, args, cwd=None, env=None, attempts=None, watch=None):
        """Run a step; on a stall the whole process tree is killed and the step retried."""
        attempts = attempts or self.STEP_ATTEMPTS
        for attempt in range(1, attempts + 1):
            self.say("$ " + " ".join(str(a) for a in args) + (f"   (attempt {attempt}/{attempts})" if attempt > 1 else ""))
            if self.job:
                self.job.check()
            try:
                code, tail = procs.run_watched(args, cwd=cwd, env=self.child_env(env), job=self.job,
                                               log_path=os.path.join(self.paths.logs, "install.log"), say=self.say,
                                               on_progress=self.on_progress, stall_warn=self.STALL_WARN,
                                               stall_kill=self.STALL_KILL, watch_dirs=[self.tmp] + (watch or []))
            except procs.Stalled as error:
                self.say(str(error))
                self.salvage()
                if attempt == attempts:
                    raise RuntimeError(f"{error} Press Retry; finished downloads are kept.") from error
                continue
            if code == 0:
                return
            if attempt == attempts or not any(k in "\n".join(tail).lower() for k in
                                              ("timed out", "connection", "incomplete", "read error", "protocolerror")):
                raise RuntimeError(f"Step failed ({code}): {' '.join(str(a) for a in args)}\n" + "\n".join(tail[-12:]))
            self.say(f"Network error; retrying ({attempt + 1}/{attempts})")
            self.salvage()

    def pip(self, *args):
        self.run([self.python, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "raw", *args])

    # MARK: big wheels: pip resolves, files land in a persistent wheelhouse, then an offline install

    def salvage(self):
        """Move in-flight wheel downloads left in TEMP into wheels/partial so the next run resumes them."""
        os.makedirs(self.partials, exist_ok=True)
        for base, _dirs, files in os.walk(self.tmp):
            for f in files:
                if f.endswith(".whl"):
                    src, dst = os.path.join(base, f), os.path.join(self.partials, f)
                    try:
                        if not os.path.exists(dst) or os.path.getsize(src) > os.path.getsize(dst):
                            shutil.move(src, dst)
                    except OSError:
                        pass

    def wheel_url(self, index, filename):
        """The file's URL on a PEP 503 simple index (follows the index's redirects)."""
        project = re.sub(r"[-_.]+", "-", filename.split("-")[0]).lower()
        page_url = index.rstrip("/") + "/" + project + "/"
        with urllib.request.urlopen(urllib.request.Request(page_url, headers={"User-Agent": "GhostLabel"}), timeout=60) as r:
            final, html = r.geturl(), r.read().decode("utf-8", "replace")
        for href in re.findall(r'href="([^"]+)"', html):
            if href.split("#")[0].rsplit("/", 1)[-1] == filename:
                return urllib.parse.urljoin(final, href.split("#")[0]), (href.split("#sha256=")[1] if "#sha256=" in href else None)
        return None, None

    def resume_partials(self, index):
        if not os.path.isdir(self.partials):
            return
        for f in sorted(os.listdir(self.partials)):
            if not f.endswith(".whl"):
                continue
            part = os.path.join(self.partials, f)
            dest = os.path.join(self.wheels, f)
            try:
                url, sha = self.wheel_url(index, f)
                if not url:
                    os.remove(part)
                    continue
                os.replace(part, dest + ".partial")
                self.say(f"Resuming {f} from {os.path.getsize(dest + '.partial') / 1e6:.0f} MB")
                download.fetch(url, dest, on_progress=lambda d, t, r, n=f: self.on_progress(n, d, t or 0, r),
                               cancelled=lambda: bool(self.job and self.job.cancel_event.is_set()), sha256=sha)
                if not wheel_ok(dest):
                    os.remove(dest)
            except Cancelled:
                raise
            except Exception as error:  # noqa: BLE001 — pip downloads it again instead
                self.say(f"Couldn't resume {f} ({error}); pip will download it again")

    def install_from_index(self, index, specs, attempts=None):
        """pip resolves and downloads into the wheelhouse (finished files are skipped next time);
        after a stall or network failure the in-flight file is resumed with HTTP Range by us."""
        os.makedirs(self.wheels, exist_ok=True)
        attempts = attempts or self.STEP_ATTEMPTS
        for attempt in range(1, attempts + 1):
            self.resume_partials(index)
            for f in os.listdir(self.wheels):
                if f.endswith(".whl") and not wheel_ok(os.path.join(self.wheels, f)):
                    self.say(f"Discarding damaged {f}")
                    os.remove(os.path.join(self.wheels, f))
            try:
                self.run([self.python, "-m", "pip", "download", "--progress-bar", "raw", "-d", self.wheels,
                          "--index-url", index, *specs], attempts=1)
                break
            except RuntimeError as error:
                self.salvage()
                if attempt == attempts:
                    raise
                self.say(f"Download interrupted ({str(error).splitlines()[0]}); resuming ({attempt + 1}/{attempts})")
        self.run([self.python, "-m", "pip", "install", "--no-index", "--find-links", self.wheels, *specs])

    # MARK: model weights: our own per-file resumable download, with totals, speed and ETA

    def hf_headers(self):
        token = self.secrets.get("hfToken") if self.secrets else ""
        return {"Authorization": f"Bearer {token}"} if token else {}

    def hf_files(self, repo, revision="main"):
        info = net.request("GET", f"{HF_ENDPOINT}/api/models/{repo}/revision/{revision}", headers=self.hf_headers(), timeout=60)
        sha = info.get("sha") or revision
        tree = net.request("GET", f"{HF_ENDPOINT}/api/models/{repo}/tree/{sha}?recursive=true", headers=self.hf_headers(), timeout=60)
        files = [{"path": f["path"], "size": int(f.get("size") or 0), "sha256": (f.get("lfs") or {}).get("oid")}
                 for f in tree if f.get("type") == "file"]
        return sha, files

    def salvage_hf_incomplete(self, dest_dir, files):
        """0.2.1 used huggingface_hub's snapshot_download; reuse its unfinished files (named
        <hash>.<sha256>.incomplete) as our partials. The sha256 check at the end catches a bad one."""
        cache = os.path.join(dest_dir, ".cache", "huggingface", "download")
        if not os.path.isdir(cache):
            return
        by_sha = {f["sha256"]: f for f in files if f["sha256"]}
        for base, _dirs, names in os.walk(cache):
            for n in names:
                if not n.endswith(".incomplete"):
                    continue
                sha = n[:-len(".incomplete")].rsplit(".", 1)[-1]
                f = by_sha.get(sha)
                if not f:
                    continue
                dest = os.path.join(dest_dir, *f["path"].split("/"))
                part = dest + ".partial"
                src = os.path.join(base, n)
                try:
                    if os.path.exists(dest) or os.path.getsize(src) > f["size"]:
                        continue
                    if not os.path.exists(part) or os.path.getsize(src) > os.path.getsize(part):
                        os.makedirs(os.path.dirname(part), exist_ok=True)
                        shutil.move(src, part)
                        self.say(f"Reusing {os.path.getsize(part) / 1e9:.2f} GB already downloaded of {f['path']}")
                except OSError:
                    pass

    def download_hf_repo(self, repo, dest_dir):
        sha, files = self.hf_files(repo)
        self.salvage_hf_incomplete(dest_dir, files)
        total = sum(f["size"] for f in files)

        def have(f):
            path = os.path.join(dest_dir, *f["path"].split("/"))
            if os.path.exists(path) and os.path.getsize(path) == f["size"]:
                return f["size"]
            return os.path.getsize(path + ".partial") if os.path.exists(path + ".partial") else 0

        finished = sum(f["size"] for f in files if os.path.exists(os.path.join(dest_dir, *f["path"].split("/")))
                       and os.path.getsize(os.path.join(dest_dir, *f["path"].split("/"))) == f["size"])
        self.say(f"Model weights: {len(files)} files, {total / 1e9:.1f} GB ({finished / 1e9:.1f} GB already here) -> {dest_dir}")
        state = {"last": time.time(), "warned": False, "started": time.time(), "start_bytes": sum(have(f) for f in files)}
        stop = threading.Event()

        def watchdog():
            while not stop.wait(5):
                idle = time.time() - state["last"]
                if self.job and idle >= self.STALL_WARN:
                    self.job.set_flags(stalled=int(idle))
                    if not state["warned"] or int(idle) % 30 < 5:
                        self.job.log(f"No progress for {int(idle)} s (downloading {state.get('name')}); retrying the connection")
                    state["warned"] = True
                elif state["warned"] and self.job:
                    self.job.set_flags(stalled=None)
                    state["warned"] = False
        threading.Thread(target=watchdog, daemon=True).start()
        lo, hi = self.step_span
        try:
            base = 0
            for f in sorted(files, key=lambda x: x["size"]):
                dest = os.path.join(dest_dir, *f["path"].split("/"))
                state["name"] = f["path"]
                if os.path.exists(dest) and os.path.getsize(dest) == f["size"]:
                    base += f["size"]
                    continue
                if os.path.exists(dest):
                    os.remove(dest)  # wrong size: an interrupted copy from somewhere else

                def progress(done, _t, rate, f=f, base=base):
                    state["last"] = time.time()
                    got = base + done
                    elapsed = max(1e-6, time.time() - state["started"])
                    avg = (got - state["start_bytes"]) / elapsed
                    eta = (total - got) / avg if avg > 0 else 0
                    if self.job:
                        self.job.progress(lo + (hi - lo) * (got / total if total else 0),
                                          f"Downloading models: {got / 1e9:.2f} / {total / 1e9:.2f} GB ({got / total * 100 if total else 0:.0f}%)"
                                          + (f" · {rate / 1e6:.1f} MB/s" if rate else "") + (f" · about {fmt_eta(eta)} left" if eta else "")
                                          + f" · {f['path'].rsplit('/', 1)[-1]}")
                for attempt in range(1, self.STEP_ATTEMPTS + 2):
                    try:
                        download.fetch_parallel(f"{HF_ENDPOINT}/{repo}/resolve/{sha}/{urllib.parse.quote(f['path'])}", dest,
                                                f["size"], on_progress=progress,
                                                cancelled=lambda: bool(self.job and self.job.cancel_event.is_set()),
                                                sha256=f["sha256"], connections=self.connections, retries=10, timeout=60,
                                                headers=self.hf_headers())
                        break
                    except Cancelled:
                        raise
                    except Exception as error:  # noqa: BLE001
                        if attempt > self.STEP_ATTEMPTS:
                            raise RuntimeError(f"Couldn't download {f['path']}: {error}. Press Retry; finished files are kept.") from error
                        self.say(f"{f['path']}: {error}; retrying ({attempt + 1})")
                base += f["size"]
                state["last"] = time.time()
        finally:
            stop.set()
            if self.job:
                self.job.set_flags(stalled=None)
        shutil.rmtree(os.path.join(dest_dir, ".cache", "huggingface", "download"), ignore_errors=True)
        self.say(f"Model weights complete in {dest_dir}")

    # MARK: steps

    def make_venv(self):
        if os.path.exists(self.python) and os.path.exists(self.marker):
            self.say("Engine Python environment already exists")
        else:
            if os.path.exists(self.venv):
                self.say("Removing an unfinished engine environment")
                shutil.rmtree(self.venv, ignore_errors=True)
            if self.uv:
                self.run([self.uv, "venv", "--python", "3.12", "--seed", self.venv])
            elif os.name == "nt" and shutil.which("py"):
                self.run(["py", "-3.12", "-m", "venv", self.venv])
            else:
                candidates = [shutil.which(n) for n in ("python3.12", "python3.11", "python3")]
                base = next((c for c in candidates if c), None)
                if not base:
                    raise RuntimeError("No Python 3.11/3.12 found and no uv to fetch one.")
                self.run([base, "-m", "venv", self.venv])
        # pip >= 25.1 for `--progress-bar raw` (byte progress) and `--resume-retries`.
        self.run([self.python, "-m", "pip", "install", "--upgrade", "pip>=25.1"])
        with open(self.marker, "w", encoding="ascii") as f:
            f.write("ok\n")

    def fetch_file(self, url, name):
        dest = os.path.join(self.downloads, name)
        lo, hi = self.step_span
        download.fetch(url, dest, on_progress=lambda d, t, r: self.on_progress(name, d, t or max(d, 1), r),
                       cancelled=lambda: bool(self.job and self.job.cancel_event.is_set()))
        return dest

    def fetch_acestep(self, ref="main"):
        if os.path.exists(os.path.join(self.ace, "acestep")):
            self.say("ACE-Step 1.5 source already present")
            return
        url = ACESTEP_ZIP.format(ref=ref)
        self.say(f"Downloading ACE-Step 1.5 source ({url})")
        zpath = self.fetch_file(url, f"ACE-Step-1.5-{ref}.zip")
        tmp = self.ace + ".partial"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            with zipfile.ZipFile(zpath) as z:
                z.extractall(tmp)
        except zipfile.BadZipFile:
            os.remove(zpath)
            raise RuntimeError("The ACE-Step download was damaged; press Retry.")
        inner = os.path.join(tmp, os.listdir(tmp)[0])
        if not os.path.isdir(self.ace):
            shutil.move(inner, self.ace)
        else:
            # The folder can already hold checkpoints/ from an earlier install: merge the source in
            # instead of moving it inside as ACE-Step-1.5/ACE-Step-1.5-main.
            for name in os.listdir(inner):
                dst = os.path.join(self.ace, name)
                if os.path.exists(dst):
                    if name == "checkpoints":
                        continue
                    if os.path.isdir(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    else:
                        os.remove(dst)
                shutil.move(os.path.join(inner, name), dst)
        shutil.rmtree(tmp, ignore_errors=True)
        if not self.layout.ace_ok():
            raise RuntimeError(f"ACE-Step's source didn't end up in {self.ace} (no acestep package). Press Retry.")

    def constraints(self):
        path = os.path.join(self.root, "torch-constraints.txt")
        out = subprocess.run([self.python, "-m", "pip", "freeze"], capture_output=True, text=True).stdout
        pins = [l for l in out.splitlines() if re.match(r"^(torch|torchvision|torchaudio)==", l)]
        with open(path, "w", encoding="ascii") as f:
            f.write("\n".join(pins) + "\n")
        self.say("Keeping " + ", ".join(pins))
        return path

    def filtered_requirements(self, name, drop=("flash-attn", "flash_attn")):
        """ACE-Step's list minus optional accelerators that need a compiler (installer policy, not taste)."""
        src = os.path.join(self.ace, name)
        dst = os.path.join(self.root, "req-" + name)
        with open(src, encoding="utf-8") as f, open(dst, "w", encoding="utf-8") as g:
            for line in f:
                if not any(line.strip().startswith(d) for d in drop):
                    g.write(line)
        return dst

    def ffmpeg(self):
        """ffmpeg for the engine's .m4a encoding: Ghost Label's own copy, PATH, winget; on Windows
        otherwise a pinned portable build is downloaded (sha256-checked) into Engine/tools/ffmpeg."""
        from smcore import runtime
        found = runtime.find_ffmpeg(self.root)
        if found:
            self.say(f"Using ffmpeg at {found}")
            return found
        if os.name != "nt":
            hint = runtime.INSTALL_HINT.get(sys.platform, runtime.INSTALL_HINT["linux"])
            self.say(f"ffmpeg not found; install it: {hint}" + (" (macOS falls back to afconvert)" if sys.platform == "darwin" else ""))
            return None
        return self.download_ffmpeg()

    def download_ffmpeg(self):
        exe = "ffmpeg.exe"
        local = os.path.join(self.tools, "ffmpeg", "bin", exe)
        if self.job:
            self.job.progress(0.0, "Downloading ffmpeg 9.0.2 (portable, ~110 MB)")
        self.say("Downloading a portable ffmpeg (gyan.dev 9.0.2 essentials)")
        zpath = self.fetch_file(FFMPEG_WIN, os.path.basename(FFMPEG_WIN))
        if self.job:
            self.job.progress(0.95, "Checking and unpacking ffmpeg")
        digest = _sha256(zpath)
        if digest != FFMPEG_WIN_SHA256:
            os.remove(zpath)
            raise RuntimeError(f"The ffmpeg download didn't match its checksum (got {digest[:12]}…); press Retry.")
        os.makedirs(self.tools, exist_ok=True)
        tmp = os.path.join(self.tools, "ffmpeg.partial")
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        inner = next((os.path.join(tmp, d) for d in os.listdir(tmp)
                      if os.path.exists(os.path.join(tmp, d, "bin", exe))), None)
        if not inner:
            raise RuntimeError("The ffmpeg download has no bin/ffmpeg.exe.")
        target = os.path.join(self.tools, "ffmpeg")
        shutil.rmtree(target, ignore_errors=True)
        shutil.move(inner, target)
        shutil.rmtree(tmp, ignore_errors=True)
        if not self._ffmpeg_runs(local):
            raise RuntimeError(f"ffmpeg at {local} doesn't run.")
        self.say(f"ffmpeg ready at {local}")
        try:
            os.remove(zpath)
        except OSError:
            pass
        return local

    @staticmethod
    def _ffmpeg_runs(path):
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            return subprocess.run([path, "-hide_banner", "-version"], capture_output=True, timeout=30,
                                  creationflags=flags).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def gpu_check(self, profile):
        out = subprocess.run([self.python, "-c", GPU_CHECK], capture_output=True, text=True)
        self.say((out.stdout or out.stderr).strip()[-400:])
        if out.returncode != 0:
            raise RuntimeError("PyTorch couldn't run a kernel: " + out.stderr.strip()[-400:])
        info = json.loads(out.stdout.strip().splitlines()[-1])
        if profile in ("rocm-windows", "cuda") and info["device"] != "cuda":
            raise RuntimeError(f"No GPU visible to PyTorch ({info}). Check the driver, then retry.")
        return info

    # MARK: ACE-Step in the venv

    def link_acestep(self):
        """Put the ACE-Step source on the venv's import path. ACE-Step's pyproject pins CUDA torch
        (torch==2.7.1+cu128 on Windows), so it must never be installed with its dependencies; a
        .pth line is the dependency-free equivalent of `pip install -e --no-deps` and needs no
        build backend or network."""
        path = self.layout.write_pth()
        if not path:
            raise RuntimeError(f"No site-packages folder in {self.venv}; the engine environment is incomplete.")
        self.say(f"Linked ACE-Step into the engine environment ({path})")

    # The modules the ACE-Step backend imports when it loads (engine/engine_host/backends/acestep.py).
    ACESTEP_CHECK = ("import sys, acestep; from acestep.handler import AceStepHandler; "
                     "from acestep.llm_inference import LLMHandler; "
                     "from acestep.model_downloader import DEFAULT_LM_MODEL, get_checkpoints_dir; import torch; "
                     "print('acestep', acestep.__file__); print('python', sys.executable); print('torch', torch.__version__)")

    def check_acestep(self):
        """Import what the backend imports, the way the engine runs: the venv's python, cwd = the
        engine host folder, no ACESTEP_PROJECT_ROOT/PYTHONPATH. Returns (ok, output)."""
        env = self.child_env()
        for k in ("ACESTEP_PROJECT_ROOT", "PYTHONPATH", "PYTHONHOME"):
            env.pop(k, None)
        try:
            out = subprocess.run([self.python, "-c", self.ACESTEP_CHECK], cwd=self.engine_dir, env=env,
                                 capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, str(error)
        return out.returncode == 0, ((out.stdout or "") + (out.stderr or "")).strip()

    def verify_acestep(self):
        self.say(f"Checking that the engine can import ACE-Step ({self.python})")
        ok, text = self.check_acestep()
        self.say(text[-600:])
        if not ok:
            raise RuntimeError(f"The engine can't import ACE-Step with {self.python}:\n{text[-800:]}\n"
                               "Press Repair engine.")

    def install_acestep_requirements(self, profile):
        if profile in ("cuda", "mps"):
            self.pip("-r", self.filtered_requirements("requirements.txt"))
        else:
            constraints = self.constraints()
            self.pip("-r", self.filtered_requirements("requirements-rocm.txt"), "-c", constraints)
        self.pip("-r", os.path.join(self.engine_dir, "requirements.txt"))
        self.install_wavelets()

    # ACE-Step's sampler applies DCW (wavelet-domain correction, on by default) only when
    # pytorch_wavelets is importable; without it the correction is skipped with a warning.
    # pytorch_wavelets is pure Python on top of torch and PyWavelets only needs numpy, so both go in
    # with --no-deps: torch and numpy are never touched.
    WAVELETS = ["pytorch_wavelets==1.3.0", "PyWavelets>=1.4"]

    def wavelets_ok(self):
        out = subprocess.run([self.python, "-c", "import pywt, pytorch_wavelets"], capture_output=True, text=True)
        return out.returncode == 0

    def install_wavelets(self):
        try:
            self.pip("--no-deps", *self.WAVELETS)
        except Exception as error:  # noqa: BLE001 — optional: the engine works without it
            self.say(f"Couldn't install pytorch_wavelets (DCW stays off): {error}")

    def install_torch(self, profile, rocm, arch):
        env_vars = {}
        if profile == "rocm-windows":
            wheels = [w.format(arch=arch) for w in ROCM_WHEELS[rocm]]
            self.install_from_index(AMD_INDEX, wheels)
            # From ACE-Step's start_gradio_ui_rocm.bat, minus HSA_OVERRIDE_GFX_VERSION (an RDNA3
            # override; gfx1151 wheels are native), plus fp32 for Strix Halo stability.
            env_vars.update(ROCM_WINDOWS_ENV)
        elif profile == "cpu":
            self.install_from_index(CPU_INDEX, ["torch", "torchvision", "torchaudio"])
        return env_vars

    def save_engine(self, profile, env_vars, extra=None):
        """Persist where the engine is, in settings.json (verified) and in a manifest next to the
        engine. The core also derives these from the layout, so a later settings edit can't lose them."""
        env = dict(env_vars)
        current = self.settings.section("engine")
        if profile == "lemonade" and current.get("profile") not in (None, "", *LIGHT) and self.layout.ace_ok():
            profile = current["profile"]   # a full install stays as it is (its ROCm environment too)
        if profile not in LIGHT:
            env["ACESTEP_PROJECT_ROOT"] = os.path.abspath(self.ace)
        merged = dict(current.get("env") or {})
        merged.update(env)
        # Installing or repairing the built-in engine keeps Lemonade selected if it was.
        backend = "mock" if profile == "mock" else "lemonade" if profile == "lemonade" or \
            current.get("backend") == "lemonade" else "acestep"
        extra = dict(extra or {})
        backend = extra.pop("selected", None) or backend
        self.settings.update({"engine": {"kind": "builtin", "backend": backend,
                                         "python": self.python, "profile": profile, "env": merged}})
        saved = self.settings.section("engine")
        if saved.get("python") != self.python or saved["env"].get("ACESTEP_PROJECT_ROOT") != env.get("ACESTEP_PROJECT_ROOT", saved["env"].get("ACESTEP_PROJECT_ROOT")):
            raise RuntimeError(f"Engine settings didn't save to {self.paths.settings}.")
        self.layout.write_manifest(dict({"python": self.python, "acestepRoot": os.path.abspath(self.ace),
                                         "profile": profile, "env": env, "saved": time.time()}, **(extra or {})))

    def repair(self, job):
        """Re-run only what's missing, keeping torch, wheels and model weights."""
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("An engine install is already running.")
        try:
            self.job = job
            return self._repair(job)
        finally:
            self.cleanup()
            self.job = None
            self.lock.release()

    def _repair(self, job):
        eng = self.settings.section("engine")
        manifest = self.layout.read_manifest()
        profile = eng.get("profile") or manifest.get("profile") or detect().get("suggested") or "cpu"
        rocm = manifest.get("rocm") or "10.0.0"
        arch = manifest.get("arch") or "gfx1151"
        done = []
        if not self.layout.python():
            raise RuntimeError("There's no engine environment to repair; press Install engine (downloaded files are reused).")
        job.progress(0.1, "Checking PyTorch")
        out = subprocess.run([self.python, "-c", "import torch; print(torch.__version__)"], capture_output=True, text=True)
        if out.returncode != 0 and profile not in LIGHT:
            self.say("PyTorch is missing; reinstalling it from the kept wheels")
            self.install_torch(profile, rocm, arch)
            done.append("reinstalled PyTorch")
        if profile in LIGHT:
            self.pip("-r", os.path.join(self.engine_dir, "requirements.txt"))
            self.save_engine(profile, {})
            return {"profile": profile, "repaired": done + ["saved engine settings"]}
        job.progress(0.25, "Checking ACE-Step's source")
        if not self.layout.ace_ok():
            self.fetch_acestep()
            done.append("downloaded ACE-Step's source")
        if not self.layout.pth_ok():
            self.link_acestep()
            done.append("linked ACE-Step into the engine environment")
        job.progress(0.45, "Checking ACE-Step's modules")
        ok, text = self.check_acestep()
        if not ok:
            self.say("ACE-Step doesn't import yet:\n" + text[-500:])
            self.say("Installing ACE-Step's requirements (torch stays as it is)")
            self.install_acestep_requirements(profile)
            done.append("installed missing ACE-Step requirements")
        if not self.wavelets_ok():
            self.say("Installing pytorch_wavelets + PyWavelets (--no-deps; torch stays as it is)")
            self.install_wavelets()
            done.append("installed pytorch_wavelets")
        n, size = self.layout.weights()
        job.progress(0.7, "Checking the model weights")
        if n == 0:
            self.download_hf_repo(ACESTEP_MAIN_REPO, self.layout.checkpoints)
            done.append("downloaded model weights")
        else:
            self.say(f"Model weights present ({n} files, {size / 1e9:.1f} GB)")
        job.progress(0.85, "Checking that the engine can load ACE-Step")
        self.verify_acestep()
        env = dict(manifest.get("env") or {})
        if profile == "rocm-windows":
            env = dict(ROCM_WINDOWS_ENV, **env)
        ffmpeg = self.ffmpeg()
        if ffmpeg:
            env["MUSIC_ENGINE_FFMPEG"] = ffmpeg
        env.pop("ACESTEP_PROJECT_ROOT", None)
        self.save_engine(profile, env, {"rocm": rocm, "arch": arch})
        done.append("saved engine settings")
        return {"profile": profile, "python": self.python, "repaired": done}

    def install(self, job, profile, rocm="10.0.0", arch="gfx1151", download_models=True, ace_ref="main"):
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile}")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Another engine install is already running.")
        try:
            self.job = job
            os.makedirs(self.root, exist_ok=True)
            return self._install(job, profile, rocm, arch, download_models, ace_ref)
        finally:
            self.cleanup()
            self.job = None
            self.lock.release()

    def cleanup(self):
        """After any run (done, failed, cancelled): keep finished wheels, park in-flight ones for
        resuming, drop temp files and pip's half-removed '~name' folders so a retry starts clean."""
        try:
            self.salvage()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        site = os.path.join(self.venv, "Lib", "site-packages") if os.name == "nt" else None
        if not site and os.path.isdir(os.path.join(self.venv, "lib")):
            site = next((os.path.join(self.venv, "lib", d, "site-packages") for d in os.listdir(os.path.join(self.venv, "lib"))), None)
        if site and os.path.isdir(site):
            for d in os.listdir(site):
                if d.startswith("~"):
                    shutil.rmtree(os.path.join(site, d), ignore_errors=True)

    def _install(self, job, profile, rocm, arch, download_models, ace_ref):
        steps = 10
        step = [0]

        def next_step(text):
            step[0] += 1
            self.step_span = (step[0] / (steps + 1), (step[0] + 1) / (steps + 1))
            job.progress(step[0] / (steps + 1), text)
            self.say(text)

        next_step("Creating the engine's Python 3.12 environment")
        self.make_venv()
        env_vars = {}
        info = {}
        if profile in LIGHT:
            next_step("Installing the engine host requirements")
            self.pip("-r", os.path.join(self.engine_dir, "requirements.txt"))
        else:
            next_step(f"Installing PyTorch ({profile})")
            env_vars = self.install_torch(profile, rocm, arch)
            next_step("Downloading ACE-Step 1.5")
            self.fetch_acestep(ace_ref)
            self.link_acestep()
            next_step("Installing ACE-Step's requirements")
            self.install_acestep_requirements(profile)
            next_step("Checking the GPU")
            info = self.gpu_check(profile)
            if profile == "rocm-windows" and not info.get("hip"):
                raise RuntimeError(f"torch was replaced by a non-ROCm build ({info.get('torch')}). Retry the install.")
        next_step("Installing yt-dlp for links")
        try:
            self.pip("yt-dlp[default]")
            ytdlp = os.path.join(os.path.dirname(self.python), "yt-dlp.exe" if os.name == "nt" else "yt-dlp")
            if os.path.exists(ytdlp):
                self.settings.update({"links": {"ytdlp": ytdlp}})
        except RuntimeError as error:
            self.say(f"yt-dlp install failed (links fall back to page metadata): {error}")
        next_step("Finding ffmpeg")
        ffmpeg = self.ffmpeg()
        if ffmpeg:
            env_vars["MUSIC_ENGINE_FFMPEG"] = ffmpeg
        if profile not in LIGHT and download_models:
            next_step("Downloading the ACE-Step models (about 10 GB; resumable)")
            self.download_hf_repo(ACESTEP_MAIN_REPO, os.path.join(self.ace, "checkpoints"))
            # ACE-Step's own downloader then finds the weights in place and only checks them.
            self.run([self.python, "-m", "acestep.model_downloader", "--model", "main"], cwd=self.ace,
                     env={"ACESTEP_PROJECT_ROOT": self.ace}, watch=[os.path.join(self.ace, "checkpoints")])
        if profile not in LIGHT:
            next_step("Checking that the engine can load ACE-Step")
            self.verify_acestep()
        next_step("Saving engine settings")
        self.save_engine(profile, env_vars, {"rocm": rocm, "arch": arch,
                                             "selected": "lemonade" if profile == "lemonade" else None})
        return {"profile": profile, "python": self.python, "gpu": info, "env": env_vars}
