"""Install robustness: single-flight installs, whole-tree cancel while a child is silent, the stall
watchdog, pip byte progress, and resumable downloads against a local slow/stalling HTTP server."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["SM_SECRET_STORE"] = "file"

from smcore import download, procs  # noqa: E402
from smcore.app import App  # noqa: E402
from smcore.bootstrap import Installer  # noqa: E402
from smcore.jobs import Cancelled  # noqa: E402
from smcore.paths import Paths  # noqa: E402
from smcore.server import serve  # noqa: E402


class FakeJob:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.messages, self.tails, self.flags, self.fractions = [], [], [], []

    def check(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def progress(self, fraction, message=None):
        self.check()
        self.fractions.append(fraction)
        if message:
            self.messages.append(message)

    def log(self, message):
        self.messages.append(message)

    def tail(self, line):
        self.tails.append(line)

    def set_flags(self, **flags):
        self.flags.append(flags)


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie still answers kill(0); check its state
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[1].split()[0] != "Z"
    except OSError:
        return False


# MARK: local file server: Range support, throttling, and a "stall once" mode

class FileServer:
    def __init__(self, files, rate=4_000_000, stall_after=None):
        self.files, self.rate, self.stall_after = files, rate, stall_after
        self.stalled_once = False
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Range")))
                if self.path.startswith("/simple/"):
                    project = self.path.strip("/").split("/")[1]
                    links = "".join(f'<a href="../../files/{n}">{n}</a>' for n in outer.files
                                    if n.split("-")[0].replace("_", "-") == project)
                    body = f"<html><body>{links}</body></html>".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                name = self.path.rsplit("/", 1)[-1]
                data = outer.files.get(name)
                if data is None:
                    self.send_error(404)
                    return
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng[6:].split("-")[0])
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                else:
                    self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data) - start))
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                pos, chunk = start, 65536
                try:
                    while pos < len(data):
                        if outer.stall_after and not outer.stalled_once and pos >= outer.stall_after:
                            outer.stalled_once = True
                            time.sleep(3600)  # a connection that stays open and sends nothing
                        self.wfile.write(data[pos:pos + chunk])
                        pos += chunk
                        time.sleep(chunk / outer.rate)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_wheel(name="bigdummy", version="1.0", size=6_000_000):
    blob = os.urandom(size)
    files = {f"{name}/__init__.py": b"VALUE = 42\n", f"{name}/blob.bin": blob,
             f"{name}-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
             f"{name}-{version}.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    import base64
    record = "".join(f"{p},sha256={base64.urlsafe_b64encode(hashlib.sha256(d).digest()).rstrip(b'=').decode()},{len(d)}\n"
                     for p, d in files.items()) + f"{name}-{version}.dist-info/RECORD,,\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for p, d in files.items():
            z.writestr(p, d)
        z.writestr(f"{name}-{version}.dist-info/RECORD", record)
    return f"{name}-{version}-py3-none-any.whl", buf.getvalue()


# MARK: single-flight

class SingleFlightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = App(home=os.path.join(self.tmp, "home"), log=lambda *_: None)
        self.release = threading.Event()
        self.calls = []

        def fake_install(job, *args, **kw):
            self.calls.append(job.id)
            while not self.release.is_set():
                job.check()
                time.sleep(0.05)
            return {"ok": True}
        self.app.installer.install = fake_install

    def tearDown(self):
        self.release.set()
        time.sleep(0.2)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, job_id, status, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            row = next((j for j in self.app.jobs.list() if j["id"] == job_id), None)
            if row and row["status"] == status:
                return row
            time.sleep(0.05)
        self.fail(f"job {job_id} never reached {status}")

    def test_second_request_attaches_to_running_job(self):
        first = self.app.install_engine("mock")
        second = self.app.install_engine("mock")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second.get("attached"))
        time.sleep(0.3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len([j for j in self.app.jobs.list() if j["kind"] == "install"]), 1)
        self.release.set()
        self.wait(first["id"], "finished")
        third = self.app.install_engine("mock")  # a finished install doesn't block a new one
        self.assertNotEqual(third["id"], first["id"])
        self.assertFalse(third.get("attached"))

    def test_over_http_from_two_windows_and_dismiss(self):
        server, token = serve(self.app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(method, path, body=None):
            req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                         headers={"X-SM-Token": token, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        try:
            results = []
            threads = [threading.Thread(target=lambda: results.append(call("POST", "/api/engine/install", {"profile": "mock"})))
                       for _ in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            ids = {r["job"]["id"] if "job" in r else r["id"] for r in results}
            self.assertEqual(len(ids), 1, results)
            time.sleep(0.3)
            self.assertEqual(len(self.calls), 1)
            job_id = ids.pop()
            self.assertFalse(call("DELETE", f"/api/jobs/{job_id}")["ok"])  # a running job can't be dismissed
            call("POST", f"/api/jobs/{job_id}/cancel")
            self.wait(job_id, "cancelled")
            self.assertTrue(call("DELETE", f"/api/jobs/{job_id}")["ok"])
            self.assertFalse(any(j["id"] == job_id for j in self.app.jobs.list()))
        finally:
            server.shutdown()

    def test_interrupted_cards_cleared_on_next_start(self):
        job = self.app.install_engine("mock")
        home = os.path.join(self.tmp, "home")
        # Simulate a quit mid-install: the next app start marks it interrupted, the one after clears it.
        second = App(home=home, log=lambda *_: None)
        self.assertEqual([j["status"] for j in second.jobs.list() if j["id"] == job["id"]], ["interrupted"])
        third = App(home=home, log=lambda *_: None)
        self.assertFalse(any(j["id"] == job["id"] for j in third.jobs.list()))


# MARK: child processes

CHILD = r"""
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(sys.argv[1], "w").write(str(g.pid))
print("Collecting torch", flush=True)
print("Downloading rocm_sdk_core-10.0.0-py3-none-win_amd64.whl (758.1 MB)", flush=True)
for i in range(int(sys.argv[2])):
    print(f"Progress {i * 1000} of 10000", flush=True)
    time.sleep(0.1)
time.sleep(600)  # silent, like pip on a dead connection
"""


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.script = os.path.join(self.tmp, "child.py")
        with open(self.script, "w") as f:
            f.write(CHILD)
        self.pidfile = os.path.join(self.tmp, "pid")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def grandchild(self):
        for _ in range(50):
            if os.path.exists(self.pidfile) and open(self.pidfile).read():
                return int(open(self.pidfile).read())
            time.sleep(0.1)
        self.fail("child never started")

    def test_cancel_while_silent_kills_whole_tree(self):
        job = FakeJob()
        threading.Timer(1.5, job.cancel_event.set).start()
        start = time.time()
        with self.assertRaises(Cancelled):
            procs.run_watched([sys.executable, self.script, self.pidfile, "3"], job=job, say=lambda *_: None,
                              stall_warn=0, stall_kill=0)
        self.assertLess(time.time() - start, 5)
        g = self.grandchild()
        time.sleep(0.3)
        self.assertFalse(alive(g), "grandchild survived the cancel")

    def test_stall_watchdog_warns_then_kills_and_progress_is_parsed(self):
        job, seen = FakeJob(), []
        start = time.time()
        with self.assertRaises(procs.Stalled) as ctx:
            procs.run_watched([sys.executable, self.script, self.pidfile, "11"], job=job, say=lambda *_: None,
                              on_progress=lambda *a: seen.append(a), stall_warn=1, stall_kill=3,
                              log_path=os.path.join(self.tmp, "log"))
        self.assertLess(time.time() - start, 12)
        self.assertIn("rocm_sdk_core", str(ctx.exception))
        self.assertTrue(seen and seen[0][0] == "rocm_sdk_core-10.0.0-py3-none-win_amd64.whl")
        self.assertEqual(seen[-1][1:3], (10000, 10000))
        self.assertTrue(any(f.get("stalled") for f in job.flags))
        self.assertTrue(any(m.startswith("No progress for") for m in job.messages))
        self.assertIn("Collecting torch", job.tails)
        self.assertFalse(any(t.startswith("Progress ") for t in job.tails))  # byte lines stay out of the tail
        time.sleep(0.3)
        self.assertFalse(alive(self.grandchild()))

    def test_growing_files_count_as_progress(self):
        watch = os.path.join(self.tmp, "w")
        os.makedirs(watch)
        writer = os.path.join(self.tmp, "writer.py")
        with open(writer, "w") as f:
            f.write(f"import time\nfor i in range(6):\n    open({os.path.join(watch, 'f')!r}, 'ab').write(b'x' * 1000)\n    time.sleep(1)\n")
        code, _ = procs.run_watched([sys.executable, writer], job=FakeJob(), say=lambda *_: None,
                                    stall_warn=1, stall_kill=3.5, watch_dirs=[watch])
        self.assertEqual(code, 0)


# MARK: downloads

class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.urandom(3_000_000)
        self.sha = hashlib.sha256(self.data).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_slow_download_reports_progress(self):
        srv = FileServer({"f.bin": self.data}, rate=1_000_000)
        try:
            seen = []
            dest = os.path.join(self.tmp, "f.bin")
            download.fetch(srv.url + "/files/f.bin", dest, on_progress=lambda *a: seen.append(a), sha256=self.sha)
            self.assertEqual(open(dest, "rb").read(), self.data)
            self.assertGreaterEqual(len(seen), 2)
            self.assertEqual(seen[-1][1], len(self.data))
        finally:
            srv.close()

    def test_stalled_connection_times_out_and_resumes_with_range(self):
        srv = FileServer({"f.bin": self.data}, rate=20_000_000, stall_after=1_000_000)
        try:
            dest = os.path.join(self.tmp, "f.bin")
            download.fetch(srv.url + "/files/f.bin", dest, timeout=1.5, sha256=self.sha)
            self.assertEqual(open(dest, "rb").read(), self.data)
            ranges = [r for _p, r in srv.requests if r]
            self.assertTrue(ranges and int(ranges[0][6:].split("-")[0]) >= 900_000, srv.requests)
        finally:
            srv.close()

    def test_existing_partial_is_resumed_and_bad_hash_rejected(self):
        srv = FileServer({"f.bin": self.data})
        try:
            dest = os.path.join(self.tmp, "f.bin")
            with open(dest + ".partial", "wb") as f:
                f.write(self.data[:2_000_000])
            download.fetch(srv.url + "/files/f.bin", dest, sha256=self.sha)
            self.assertEqual(srv.requests[-1][1], "bytes=2000000-")
            self.assertEqual(open(dest, "rb").read(), self.data)
            with self.assertRaises(Exception):
                download.fetch(srv.url + "/files/f.bin", os.path.join(self.tmp, "g.bin"), sha256="0" * 64, retries=1)
        finally:
            srv.close()

    def test_cancel_stops_download(self):
        srv = FileServer({"f.bin": self.data}, rate=500_000)
        try:
            flag = threading.Event()
            threading.Timer(0.5, flag.set).start()
            with self.assertRaises(Cancelled):
                download.fetch(srv.url + "/files/f.bin", os.path.join(self.tmp, "f.bin"), cancelled=flag.is_set)
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "f.bin.partial")))  # kept for a retry
        finally:
            srv.close()


# MARK: real pip against a local slow index: byte progress, stall kill, salvage + resume, offline install

def pip_version(python):
    out = subprocess.run([python, "-m", "pip", "--version"], capture_output=True, text=True).stdout
    try:
        return tuple(int(x) for x in out.split()[1].split(".")[:2])
    except (IndexError, ValueError):
        return (0, 0)


class PipInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.paths = Paths(os.path.join(cls.tmp, "home"))
        venv = os.path.join(cls.paths.envs, "venv")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True, capture_output=True)
        cls.python = os.path.join(venv, "bin", "python")
        if pip_version(cls.python) < (25, 1):
            raise unittest.SkipTest("needs pip >= 25.1 in a fresh venv")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_stalled_wheel_is_resumed_then_installed_offline(self):
        name, data = make_wheel(size=6_000_000)
        srv = FileServer({name: data}, rate=6_000_000, stall_after=2_500_000)
        inst = Installer(settings=None, paths=self.paths, engine_dir=self.tmp, log=lambda *_: None)
        inst.STALL_WARN, inst.STALL_KILL = 1, 4
        inst.job = job = FakeJob()
        inst.step_span = (0.2, 0.3)
        try:
            inst.install_from_index(srv.url + "/simple/", ["bigdummy==1.0"])
            inst.cleanup()
            out = subprocess.run([inst.python, "-c", "import bigdummy; print(bigdummy.VALUE)"], capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "42", out.stderr)
            self.assertTrue(os.path.exists(os.path.join(inst.wheels, name)))
            self.assertTrue(srv.stalled_once)
            self.assertTrue(any(r and int(r[6:].split("-")[0]) > 1_000_000 for _p, r in srv.requests), srv.requests)
            self.assertTrue(any("Downloading bigdummy" in m and "MB (" in m for m in job.messages), job.messages)
            self.assertTrue(any(m.startswith("No progress for") for m in job.messages))
            self.assertTrue(all(0.2 <= f <= 0.3 for f in job.fractions))
            self.assertFalse(os.path.exists(inst.tmp))
            # A second install reuses the wheelhouse: no new file download.
            before = len([p for p, _r in srv.requests if p.startswith("/files/")])
            inst.install_from_index(srv.url + "/simple/", ["bigdummy==1.0"])
            self.assertEqual(len([p for p, _r in srv.requests if p.startswith("/files/")]), before)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()


# MARK: model weights from a fake Hugging Face endpoint

class HFServer(FileServer):
    """Serves /api/models/<repo>/revision/main, /api/models/<repo>/tree/<sha>, and
    /<repo>/resolve/<sha>/<path> (Range-capable, throttled, optional one-time stall)."""

    def __init__(self, repo, files, **kw):
        self.repo, self.blobs = repo, files
        super().__init__({}, **kw)
        outer = self
        handler = self.httpd.RequestHandlerClass
        original = handler.do_GET

        def do_GET(h):
            path = urllib.parse.unquote(h.path)
            if path == f"/api/models/{repo}/revision/main":
                body = json.dumps({"sha": "abc123"}).encode()
            elif path.startswith(f"/api/models/{repo}/tree/abc123"):
                body = json.dumps([{"type": "file", "path": p, "size": len(d),
                                    **({"lfs": {"oid": hashlib.sha256(d).hexdigest(), "size": len(d)}} if len(d) > 1000 else {})}
                                   for p, d in outer.blobs.items()] + [{"type": "directory", "path": "vae"}]).encode()
            else:
                prefix = f"/{repo}/resolve/abc123/"
                if path.startswith(prefix):
                    name = path[len(prefix):]
                    outer.files[h.path.rsplit("/", 1)[-1]] = outer.blobs[name]
                return original(h)
            outer.requests.append((h.path, None))
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        handler.do_GET = do_GET


class ModelDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = Paths(os.path.join(self.tmp, "home"))
        self.blobs = {"README.md": b"# readme\n", "vae/config.json": b"{}",
                      "vae/diffusion_pytorch_model.safetensors": os.urandom(2_500_000),
                      "acestep-v15-turbo/model.safetensors": os.urandom(4_000_000)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def installer(self):
        inst = Installer(settings=None, paths=self.paths, engine_dir=self.tmp, log=lambda *_: None)
        inst.job = FakeJob()
        inst.step_span = (0.8, 0.9)
        return inst

    def test_downloads_resumes_salvages_and_skips_finished(self):
        import smcore.bootstrap as bs
        srv = HFServer("ACE-Step/Ace-Step1.5", self.blobs, rate=8_000_000, stall_after=1_500_000)
        orig = bs.HF_ENDPOINT
        bs.HF_ENDPOINT = srv.url
        dest = os.path.join(self.tmp, "checkpoints")
        # Left over by 0.2.1's snapshot_download: an unfinished file under .cache/huggingface/download.
        vae = self.blobs["vae/diffusion_pytorch_model.safetensors"]
        inc = os.path.join(dest, ".cache", "huggingface", "download", "vae")
        os.makedirs(inc)
        with open(os.path.join(inc, f"Xyz.{hashlib.sha256(vae).hexdigest()}.incomplete"), "wb") as f:
            f.write(vae[:1_000_000])
        try:
            inst = self.installer()
            # Short socket timeout so the one-time stall turns into a resumed request quickly.
            real_fetch = bs.download.fetch
            bs.download.fetch = lambda *a, **k: real_fetch(*a, **{**k, "timeout": 1.5})
            try:
                inst.download_hf_repo("ACE-Step/Ace-Step1.5", dest)
            finally:
                bs.download.fetch = real_fetch
            for p, d in self.blobs.items():
                with open(os.path.join(dest, *p.split("/")), "rb") as f:
                    self.assertEqual(f.read(), d, p)
            ranges = [r for _p, r in srv.requests if r]
            self.assertIn("bytes=1000000-", ranges)           # the salvaged partial was resumed
            self.assertTrue(srv.stalled_once)
            self.assertTrue(len(ranges) >= 2, srv.requests)     # and the stalled one too
            msgs = [m for m in inst.job.messages if m.startswith("Downloading models:")]
            self.assertTrue(msgs and "GB" in msgs[-1] and "%" in msgs[-1], inst.job.messages)
            self.assertTrue(all(0.8 <= f <= 0.9 for f in inst.job.fractions))
            self.assertFalse(os.path.exists(os.path.join(dest, ".cache", "huggingface", "download")))
            before = len([p for p, _r in srv.requests if "/resolve/" in p])
            self.installer().download_hf_repo("ACE-Step/Ace-Step1.5", dest)   # everything in place: no downloads
            self.assertEqual(len([p for p, _r in srv.requests if "/resolve/" in p]), before)
        finally:
            bs.HF_ENDPOINT = orig
            srv.close()


class AnsiTests(unittest.TestCase):
    def test_colors_are_stripped_from_tail(self):
        job = FakeJob()
        code, tail = procs.run_watched([sys.executable, "-c", "print('\\x1b[32m2026-11-10\\x1b[0m | \\x1b[1mINFO\\x1b[0m Downloading from HuggingFace')"],
                                       job=job, say=lambda *_: None, stall_warn=0, stall_kill=0)
        self.assertEqual(code, 0)
        self.assertEqual(tail, ["2026-11-10 | INFO Downloading from HuggingFace"])


class ParallelDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.urandom(12_000_000)
        self.sha = hashlib.sha256(self.data).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parallel_is_faster_than_one_capped_connection(self):
        srv = FileServer({"f.bin": self.data}, rate=3_000_000)   # each connection capped at ~3 MB/s
        try:
            seen = []
            t = time.time()
            dest = download.fetch_parallel(srv.url + "/files/f.bin", os.path.join(self.tmp, "p.bin"), len(self.data),
                                           on_progress=lambda *a: seen.append(a), sha256=self.sha, connections=6, piece=1 << 20)
            parallel = time.time() - t
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), self.data)
            self.assertLess(parallel, 4 * 0.6)   # one connection needs ~4 s
            self.assertGreater(max(a[2] for a in seen), 6_000_000)
            self.assertEqual(seen[-1][:2], (len(self.data), len(self.data)))
            self.assertFalse(os.path.exists(dest + ".partial.json"))
        finally:
            srv.close()

    def test_cancel_then_resume_reuses_finished_pieces(self):
        srv = FileServer({"f.bin": self.data}, rate=2_000_000)
        dest = os.path.join(self.tmp, "r.bin")
        try:
            flag = threading.Event()
            threading.Timer(1.5, flag.set).start()
            with self.assertRaises(Cancelled):
                download.fetch_parallel(srv.url + "/files/f.bin", dest, len(self.data), cancelled=flag.is_set,
                                        sha256=self.sha, connections=4, piece=1 << 20)
            with open(dest + ".partial.json") as f:
                finished = json.load(f)["done"]
            self.assertTrue(finished)
            srv.requests.clear()
            download.fetch_parallel(srv.url + "/files/f.bin", dest, len(self.data), sha256=self.sha, connections=4, piece=1 << 20)
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), self.data)
            starts = {int(r[6:].split("-")[0]) for _p, r in srv.requests if r}
            self.assertFalse(any(i * (1 << 20) in starts for i in finished))   # finished pieces not fetched again
        finally:
            srv.close()

    def test_sequential_partial_from_another_downloader_counts(self):
        srv = FileServer({"f.bin": self.data}, rate=50_000_000)
        dest = os.path.join(self.tmp, "s.bin")
        with open(dest + ".partial", "wb") as f:
            f.write(self.data[:5_500_000])   # e.g. a salvaged hf_xet .incomplete
        try:
            download.fetch_parallel(srv.url + "/files/f.bin", dest, len(self.data), sha256=self.sha, connections=4, piece=1 << 20)
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), self.data)
            starts = sorted(int(r[6:].split("-")[0]) for _p, r in srv.requests if r)
            self.assertEqual(starts[0], 5 * (1 << 20))   # only pieces after the finished prefix
        finally:
            srv.close()

    def test_bad_hash_is_rejected_and_cleared(self):
        srv = FileServer({"f.bin": self.data}, rate=50_000_000)
        dest = os.path.join(self.tmp, "b.bin")
        try:
            with self.assertRaises(IOError):
                download.fetch_parallel(srv.url + "/files/f.bin", dest, len(self.data), sha256="0" * 64, connections=4, piece=1 << 20)
            self.assertFalse(os.path.exists(dest + ".partial"))
            self.assertFalse(os.path.exists(dest))
        finally:
            srv.close()
