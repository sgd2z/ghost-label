"""End-to-end and unit tests. Run from the repo root:  python -m unittest discover -s tests -v

The protocol tests launch engine.py as a subprocess exactly as Standard Music does (JSON lines on
stdin/stdout) and check the .m4a with ffprobe when it is installed. The ACE-Step tests run the
real backend module against a stub of ACE-Step's Python API (tests/fakes), so the parameter
mapping is checked without the model."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine.py")
DRIVER = os.path.join(ROOT, "driver.py")
FAKES = os.path.join(ROOT, "tests", "fakes")
FAKE_TORCH = os.path.join(ROOT, "tests", "fakes_torch")
sys.path.insert(0, ROOT)

from engine_host import audio, backends  # noqa: E402

try:
    import torch  # noqa: F401
    HAVE_TORCH = True
except ImportError:
    HAVE_TORCH = False

FFMPEG = audio.find_ffmpeg()
FFPROBE = shutil.which("ffprobe")
# macOS without ffmpeg falls back to afconvert, like the app's reference engine.
CAN_ENCODE = bool(FFMPEG) or (sys.platform == "darwin" and os.path.exists(audio.AFCONVERT))


def job(out, jid="t1", **extra):
    j = {"id": jid, "style": "English, indie rock, female vocal", "lyrics": "[Verse]\nHello\n[Chorus]\nTurn it up",
         "planning": "full", "seed": 7, "steps": 16, "maxSemanticTokens": 8250, "outputPath": out,
         "title": "Turn It Up", "artist": "The Halts", "voice": "warm alto", "isInstrumental": False,
         "maxSeconds": 20}
    j.update(extra)
    return {"generate": {"_0": j}}


class EngineProcess:
    """engine.py as the app runs it: cwd = its folder, MLX_ENABLE_TF32=0, pipes on stdio."""

    def __init__(self, args, env=None):
        e = dict(os.environ)
        e["MLX_ENABLE_TF32"] = "0"
        e.update(env or {})
        self.proc = subprocess.Popen([sys.executable, ENGINE] + args, cwd=ROOT, env=e,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.lines = []

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def read_until(self, key, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.proc.stdout.readline()
            if not raw:
                raise AssertionError("engine exited: " + self.proc.stderr.read().decode()[-2000:])
            event = json.loads(raw)  # every stdout line must be JSON
            self.lines.append(event)
            if key in event:
                return event
        raise AssertionError(f"timed out waiting for {key}")

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc.stdout.close()
        self.proc.stderr.close()
        return self.proc.returncode


def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                          "format=format_name,duration:stream=codec_name,sample_rate,channels",
                          "-of", "json", path], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(CAN_ENCODE, "no ffmpeg (or afconvert on macOS)")
class MockProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = EngineProcess(["--backend", "mock", "--set", "delay=0.3"])

    def tearDown(self):
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_generate_finish(self):
        e = self.engine
        e.send({"load": {"modelPath": ROOT}})  # the app sends the program's folder when Model is empty
        ready = e.read_until("ready")
        self.assertIn("loadSeconds", ready["ready"])
        out = os.path.join(self.tmp, "Audio", "song.m4a")
        e.send(job(out))
        done = e.read_until("finished")["finished"]["_0"]
        self.assertEqual(done["id"], "t1")
        self.assertEqual(set(done), {"id", "duration", "loudness", "seconds", "truncated"})
        self.assertAlmostEqual(done["duration"], 6.0, delta=0.05)  # 0.5 s of leading silence trimmed
        self.assertFalse(done["truncated"])
        self.assertTrue(-30 < done["loudness"] < 0)
        progress = [p["progress"]["_1"] for p in e.lines if "progress" in p]
        self.assertTrue(progress)
        overall = [p["overall"] for p in progress]
        self.assertEqual(overall, sorted(overall))
        self.assertTrue(all(0 <= v <= 1 for v in overall))
        self.assertTrue({"planning", "composing", "rendering", "encoding"} <= {p["stage"] for p in progress})
        self.assertTrue(all(p["id"] == "t1" for p in (x["progress"] for x in e.lines if "progress" in x)))
        self.assertTrue(os.path.exists(out))
        self.assertEqual([f for f in os.listdir(os.path.dirname(out)) if "partial" in f], [])
        if FFPROBE:
            info = probe(out)
            self.assertEqual(info["streams"][0]["codec_name"], "aac")
            self.assertEqual(info["streams"][0]["channels"], 2)
            self.assertEqual(int(info["streams"][0]["sample_rate"]), 48000)
            self.assertIn("m4a", info["format"]["format_name"])
        self.assertTrue(ready["ready"]["capabilities"]["analyze"])
        # A second load of the same model is instant.
        e.send({"load": {"modelPath": ""}})
        self.assertEqual(e.read_until("ready")["ready"]["loadSeconds"], 0)

    def test_truncates_with_fade_at_max_seconds(self):
        self.engine.close()
        self.engine = EngineProcess(["--backend", "mock", "--set", "seconds=12", "--set", "delay=0"])
        self.engine.send({"load": {"modelPath": ""}})
        self.engine.read_until("ready")
        out = os.path.join(self.tmp, "cut.m4a")
        self.engine.send(job(out, maxSeconds=4))
        done = self.engine.read_until("finished")["finished"]["_0"]
        self.assertTrue(done["truncated"])
        self.assertLessEqual(done["duration"], 4.0)

    def test_cancel_while_rendering(self):
        e = self.engine
        e.send({"load": {"modelPath": ""}})
        e.read_until("ready")
        out = os.path.join(self.tmp, "cancel.m4a")
        e.send(job(out, jid="c1"))
        e.read_until("progress")
        e.send({"cancel": {"id": "c1"}})
        self.assertEqual(e.read_until("cancelled")["cancelled"]["id"], "c1")
        self.assertFalse(os.path.exists(out))
        # still alive afterwards
        e.send(job(os.path.join(self.tmp, "after.m4a"), jid="c2"))
        self.assertEqual(e.read_until("finished")["finished"]["_0"]["id"], "c2")

    def test_cancel_before_generate(self):
        e = self.engine
        e.send({"load": {"modelPath": ""}})
        e.read_until("ready")
        e.send({"cancel": {"id": "early"}})
        time.sleep(0.2)
        e.send(job(os.path.join(self.tmp, "early.m4a"), jid="early"))
        self.assertEqual(e.read_until("cancelled")["cancelled"]["id"], "early")

    def test_shutdown_exits(self):
        self.engine.send({"shutdown": {}})
        self.assertEqual(self.engine.proc.wait(timeout=10), 0)

    def test_stdin_closed_exits(self):
        self.engine.proc.stdin.close()
        self.assertEqual(self.engine.proc.wait(timeout=10), 0)


@unittest.skipUnless(CAN_ENCODE, "no ffmpeg (or afconvert on macOS)")
class MockFailureTests(unittest.TestCase):
    def test_generate_failure_is_reported_and_engine_survives(self):
        tmp = tempfile.mkdtemp()
        e = EngineProcess(["--backend", "mock", "--set", "delay=0"], env={"MUSIC_ENGINE_MOCK_FAIL": "1"})
        try:
            e.send({"load": {"modelPath": ""}})
            e.read_until("ready")
            e.send(job(os.path.join(tmp, "x.m4a"), jid="f1"))
            failed = e.read_until("failed")["failed"]
            self.assertEqual(failed["id"], "f1")
            self.assertIn("on purpose", failed["message"])
            e.send({"load": {"modelPath": ""}})
            e.read_until("ready")
        finally:
            e.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_failure_has_no_id(self):
        e = EngineProcess(["--backend", "mock", "--set", "fail_load=1"])
        try:
            e.send({"load": {"modelPath": "some/model"}})
            failed = e.read_until("failed")["failed"]
            self.assertNotIn("id", failed)
            self.assertIn("Couldn't load the model", failed["message"])
        finally:
            e.close()

    def test_unknown_backend_still_speaks_protocol(self):
        e = EngineProcess(["--backend", "nope"])
        try:
            e.send({"load": {"modelPath": ""}})
            self.assertIn("nope", e.read_until("failed")["failed"]["message"])
        finally:
            e.close()

    def test_config_file_selects_backend_and_options(self):
        tmp = tempfile.mkdtemp()
        cfg = os.path.join(tmp, "engine.json")
        with open(cfg, "w", encoding="utf-8-sig") as f:  # with a BOM, as PowerShell 5 writes it
            json.dump({"backend": "mock", "options": {"seconds": "3", "delay": "0"}}, f)
        e = EngineProcess(["--config", cfg])
        try:
            e.send({"load": {"modelPath": ""}})
            e.read_until("ready")
            e.send(job(os.path.join(tmp, "cfg.m4a")))
            self.assertAlmostEqual(e.read_until("finished")["finished"]["_0"]["duration"], 3.0, delta=0.05)
        finally:
            e.close()
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(CAN_ENCODE, "no ffmpeg (or afconvert on macOS)")
class DriverTests(unittest.TestCase):
    def test_driver_round_trip(self):
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, "driver song.m4a")
        try:
            r = subprocess.run([sys.executable, DRIVER, "--engine-arg=--backend", "--engine-arg=mock",
                                "--engine-arg=--set", "--engine-arg=delay=0", "--max-seconds", "5",
                                "--out", out, "--no-play"], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), out)
            self.assertIn("finished", r.stderr)
            self.assertTrue(os.path.exists(out))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_driver_cancel(self):
        r = subprocess.run([sys.executable, DRIVER, "--engine-arg=--backend", "--engine-arg=mock",
                            "--engine-arg=--set", "--engine-arg=delay=5", "--cancel-after", "0.5",
                            "--no-play"], capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cancelled", r.stderr)


def fake_acestep_env(log_path, **extra):
    paths = [FAKES] + ([] if HAVE_TORCH else [FAKE_TORCH])
    env = {"PYTHONPATH": os.pathsep.join(paths + [os.environ.get("PYTHONPATH", "")]),
           "FAKE_ACESTEP_LOG": log_path, "ACESTEP_PROJECT_ROOT": FAKES, "MUSIC_ENGINE_DEVICE": "cpu"}
    env.update(extra)
    return env


def read_log(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


@unittest.skipUnless(CAN_ENCODE, "no ffmpeg (or afconvert on macOS)")
class AceStepBackendWithStubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "calls.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_song(self, extra_args=(), env=None, model="", **job_fields):
        e = EngineProcess(["--backend", "acestep", *extra_args], env=fake_acestep_env(self.log, **(env or {})))
        try:
            e.send({"load": {"modelPath": model}})
            e.read_until("ready")
            out = os.path.join(self.tmp, "a.m4a")
            e.send(job(out, **job_fields))
            event = e.read_until("finished") if "expect" not in job_fields else None
            return e, event, out
        except Exception:
            e.close()
            raise

    def test_mapping_turbo_defaults(self):
        e, event, out = self.run_song(steps=32, seed=2**40 + 5)
        e.close()
        calls = read_log(self.log)
        init = next(c for c in calls if c["event"] == "initialize_service")
        self.assertEqual(init["config_path"], "acestep-v15-turbo")
        self.assertEqual(init["device"], "cpu")
        lm = next(c for c in calls if c["event"] == "llm_initialize")
        self.assertEqual(lm["lm_model_path"], "acestep-5Hz-lm-1.7B")
        self.assertEqual(lm["backend"], "pt")
        gen = next(c for c in calls if c["event"] == "generate_music")
        p, c = gen["params"], gen["config"]
        # The voice is added to the caption (ACE-Step has no voice input), once.
        self.assertEqual(p["caption"], "English, indie rock, female vocal, warm alto")
        self.assertEqual(p["lyrics"], "[Verse]\nHello\n[Chorus]\nTurn it up")
        self.assertFalse(p["instrumental"])
        self.assertEqual(p["inference_steps"], 20)          # quality 32 -> 20 turbo steps
        self.assertEqual(p["seed"], (2**40 + 5) % 2**31)
        self.assertEqual(p["shift"], 3.0)
        self.assertEqual(p["duration"], -1.0)               # auto
        self.assertTrue(p["thinking"])
        self.assertFalse(p["use_cot_caption"])              # the app's style is kept as the caption
        self.assertEqual(c["batch_size"], 1)
        self.assertEqual(c["seeds"], [p["seed"]])
        self.assertFalse(c["use_random_seed"])
        self.assertIsNone(gen["save_dir"])
        self.assertAlmostEqual(event["finished"]["_0"]["duration"], 5.0, delta=0.05)
        self.assertEqual(probe(out)["streams"][0]["codec_name"], "aac") if FFPROBE else None

    def test_extensions_language_reference_bpm_and_analyze(self):
        ref = os.path.join(self.tmp, "ref.wav")
        open(ref, "wb").close()
        e, event, _ = self.run_song(vocalLanguage="hi", referenceAudio=ref, bpm=72, keyscale="E minor")
        try:
            gen = next(c for c in read_log(self.log) if c["event"] == "generate_music")["params"]
            self.assertEqual(gen["vocal_language"], "hi")
            self.assertEqual(gen["reference_audio"], ref)
            self.assertEqual(gen["bpm"], 72)
            self.assertEqual(gen["keyscale"], "E minor")
            e.send({"load": {"modelPath": ""}})
            caps = e.read_until("ready")["ready"]["capabilities"]
            self.assertTrue(caps["analyze"] and caps["referenceAudio"])
            self.assertIn("ur", caps["languages"])
            e.send({"analyze": {"_0": {"id": "a1", "audioPath": ref}}})
            result = e.read_until("analyzed")["analyzed"]["_0"]
            self.assertEqual(result["id"], "a1")
            self.assertEqual(result["result"]["language"], "hi")
            self.assertEqual(result["result"]["bpm"], 72)
            e.send({"analyze": {"_0": {"id": "a2", "audioPath": ref + ".missing"}}})
            failed = e.read_until("failed")["failed"]
            self.assertEqual(failed["id"], "a2")
        finally:
            e.close()

    def test_mapping_instrumental_base_model_max_duration_no_lm(self):
        e, event, _ = self.run_song(
            extra_args=["--set", "duration=max", "--set", "init_llm=false"], model="acestep-v15-base",
            steps=8, isInstrumental=True, planning="off", maxSeconds=30)
        e.close()
        calls = read_log(self.log)
        self.assertFalse(any(c["event"] == "llm_initialize" for c in calls))
        init = next(c for c in calls if c["event"] == "initialize_service")
        self.assertEqual(init["config_path"], "acestep-v15-base")
        p = next(c for c in calls if c["event"] == "generate_music")["params"]
        self.assertEqual(p["lyrics"], "[Instrumental]")
        self.assertTrue(p["instrumental"])
        self.assertEqual(p["inference_steps"], 32)          # base model, quality 8
        self.assertEqual(p["shift"], 1.0)
        self.assertEqual(p["duration"], 30.0)
        self.assertFalse(p["thinking"])
        self.assertTrue(event["finished"]["_0"]["truncated"])

    def test_model_folder_sets_checkpoints_dir(self):
        model_dir = os.path.join(self.tmp, "ckpts", "acestep-v15-xl-turbo")
        os.makedirs(model_dir)
        open(os.path.join(model_dir, "config.json"), "w").close()
        e, _, _ = self.run_song(model=model_dir, steps=16, extra_args=["--set", "steps_map=8:4,16:6,32:10"])
        e.close()
        calls = read_log(self.log)
        init = next(c for c in calls if c["event"] == "initialize_service")
        self.assertEqual(init["config_path"], "acestep-v15-xl-turbo")
        self.assertEqual(init["checkpoints"], os.path.join(self.tmp, "ckpts"))
        p = next(c for c in calls if c["event"] == "generate_music")["params"]
        self.assertEqual(p["inference_steps"], 6)

    def test_plain_folder_is_not_used_as_checkpoints_and_registered_lm_is_downloaded(self):
        plain = os.path.join(self.tmp, "just a folder")
        os.makedirs(plain)
        e, _, _ = self.run_song(model=plain, extra_args=["--set", "lm_model_path=acestep-5Hz-lm-4B"])
        e.close()
        calls = read_log(self.log)
        init = next(c for c in calls if c["event"] == "initialize_service")
        self.assertEqual(init["config_path"], "acestep-v15-turbo")
        self.assertIsNone(init["checkpoints"])
        self.assertTrue(any(c["event"] == "ensure_lm_model" and c["model_name"] == "acestep-5Hz-lm-4B"
                            for c in calls))

    def test_cancel_and_failures(self):
        e = EngineProcess(["--backend", "acestep"], env=fake_acestep_env(self.log, FAKE_ACESTEP_DELAY="0.5"))
        try:
            e.send({"load": {"modelPath": ""}})
            e.read_until("ready")
            e.send(job(os.path.join(self.tmp, "c.m4a"), jid="c1"))
            e.read_until("progress")
            e.send({"cancel": {"id": "c1"}})
            self.assertEqual(e.read_until("cancelled")["cancelled"]["id"], "c1")
            e.send(job(os.path.join(self.tmp, "f.m4a"), jid="f1", style="fail please", voice=""))
            failed = e.read_until("failed")["failed"]
            self.assertEqual(failed, {"id": "f1", "message": "stub failure"})
        finally:
            e.close()
        e = EngineProcess(["--backend", "acestep"], env=fake_acestep_env(self.log))
        try:
            e.send({"load": {"modelPath": "broken-model"}})
            self.assertIn("broken-model", e.read_until("failed")["failed"]["message"])
        finally:
            e.close()

    def test_progress_stages_follow_acestep_scale(self):
        e, _, _ = self.run_song()
        e.close()
        stages = [x["progress"]["_1"]["stage"] for x in e.lines if "progress" in x]
        for stage in ("planning", "composing", "rendering", "encoding"):
            self.assertIn(stage, stages)

    def test_decode_reports_its_own_stage_and_chunks(self):
        e, _, _ = self.run_song(env={"FAKE_ACESTEP_DECODE_DELAY": "1.2"})
        e.close()
        progress = [x["progress"]["_1"] for x in e.lines if "progress" in x]
        decoding = [p for p in progress if p["stage"] == "rendering" and p.get("chunks")]
        self.assertTrue(decoding)
        self.assertTrue(all(p["chunks"] == 3 and 1 <= p["chunk"] <= 3 for p in decoding))
        self.assertTrue(all(p["detail"].startswith("Decoding the audio") for p in decoding))
        self.assertEqual(sorted({p["chunk"] for p in decoding}), [1, 2, 3])
        # Once decoding started, ACE-Step's own later values don't move the stage back.
        after = progress[progress.index(decoding[0]):]
        self.assertNotIn("composing", {p["stage"] for p in after})


class CaptionTests(unittest.TestCase):
    def test_voice_goes_in_the_caption_once(self):
        from engine_host.backends.acestep import caption_with_voice
        self.assertEqual(caption_with_voice("bollywood pop, strings", "female vocals, breathy soprano"),
                         "bollywood pop, strings, female vocals, breathy soprano")
        self.assertEqual(caption_with_voice("pop, Female Vocals, Breathy Soprano", "female vocals, breathy soprano"),
                         "pop, Female Vocals, Breathy Soprano")
        self.assertEqual(caption_with_voice("ambient", "instrumental"), "ambient")
        self.assertEqual(caption_with_voice("ambient", None), "ambient")


class UnitTests(unittest.TestCase):
    def test_as_frames(self):
        self.assertEqual(audio.as_frames(np.zeros((2, 1000))).shape, (1000, 2))
        self.assertEqual(audio.as_frames(np.zeros(1000)).shape, (1000, 2))
        self.assertEqual(audio.as_frames(np.zeros((1000, 2))).shape, (1000, 2))

    def test_trim_and_cut(self):
        x = np.concatenate([np.zeros((480, 2)), 0.5 * np.ones((4800, 2))]).astype(np.float32)
        self.assertEqual(len(audio.trim_leading_silence(x)), 4800)
        y, cut = audio.cut_and_fade(np.ones((48000 * 5, 2), np.float32), 48000, 4.0, 1.0)
        self.assertTrue(cut)
        self.assertEqual(len(y), 48000 * 4)
        self.assertAlmostEqual(float(y[-1, 0]), 0.0)
        self.assertAlmostEqual(float(y[0, 0]), 1.0)

    def test_loudness(self):
        self.assertEqual(audio.loudness(np.zeros((48000, 2), np.float32)), -70.0)
        sine = 0.5 * np.sin(np.linspace(0, 2 * np.pi * 440, 48000 * 2)).astype(np.float32)
        self.assertAlmostEqual(audio.loudness(np.stack([sine, sine], 1)), -0.691 + 10 * np.log10(0.25), delta=0.2)

    def test_steps_map(self):
        self.assertEqual(backends.parse_steps_map("8:8, 16:12,32:20"), {8: 8, 16: 12, 32: 20})
        b = backends.create("mock", {})
        self.assertEqual(b.map_steps(16), 4)
        self.assertEqual(b.map_steps(20), 4)   # nearest setting
        b = backends.create("mock", {"steps_map": "8:1,16:2,32:3"})
        self.assertEqual(b.map_steps(32), 3)

    def test_registry(self):
        self.assertIn("acestep", backends.BACKENDS)
        with self.assertRaises(ValueError):
            backends.create("nope")
        self.assertEqual(backends.create("engine_host.backends.mock:MockBackend").name, "mock")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(CAN_ENCODE, "no ffmpeg (or afconvert on macOS)")
class LiveProgressTests(unittest.TestCase):
    def test_loading_heartbeat_and_real_counters(self):
        tmp = tempfile.mkdtemp()
        e = EngineProcess(["--backend", "mock", "--set", "delay=1.5", "--set", "load_delay=4.5"])
        try:
            e.send({"load": {"modelPath": ROOT}})
            e.read_until("ready")
            loading = [m["loading"] for m in e.lines if "loading" in m]
            self.assertGreaterEqual(len(loading), 2)  # the status line, then heartbeats every 2 s
            self.assertEqual(loading[0]["stage"], "Loading test tones")
            self.assertEqual([x["elapsed"] for x in loading], sorted(x["elapsed"] for x in loading))
            e.send(job(os.path.join(tmp, "a.m4a"), steps=16))
            e.read_until("finished")
            progress = [m["progress"]["_1"] for m in e.lines if "progress" in m]
            steps = [p for p in progress if p["stage"] == "composing" and "step" in p]
            self.assertTrue(steps)
            self.assertTrue(all(p["steps"] == 4 and 1 <= p["step"] <= 4 for p in steps))
            self.assertTrue(any("tokens" in p for p in progress if p["stage"] == "planning"))
        finally:
            e.close()
            shutil.rmtree(tmp, ignore_errors=True)


class VramCheckTests(unittest.TestCase):
    def fake_torch(self, free_gb, total_gb=64, hip="6.4"):
        class Cuda:
            @staticmethod
            def mem_get_info():
                return int(free_gb * 2**30), int(total_gb * 2**30)

        class Version:
            pass
        t = type("T", (), {})()
        t.cuda = Cuda
        t.version = Version()
        t.version.hip = hip
        return t

    def checkpoints(self, gb_dit=0.5):
        import struct
        root = tempfile.mkdtemp()
        for part, size in (("acestep-v15-turbo", gb_dit), ("vae", 0.1), ("Qwen3-Embedding-0.6B", 0.1),
                           ("acestep-5Hz-lm-1.7B", 0.2)):
            os.makedirs(os.path.join(root, part))
            header = json.dumps({"w": {"dtype": "BF16", "shape": [1], "data_offsets": [0, 2]}}).encode()
            with open(os.path.join(root, part, "model.safetensors"), "wb") as f:
                f.write(struct.pack("<Q", len(header)) + header)
                f.truncate(int(size * 2**30))
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def test_shortfall_names_the_numbers_and_the_fix(self):
        from engine_host.backends.acestep import check_vram
        root = self.checkpoints()
        logs = []
        with self.assertRaises(RuntimeError) as ctx:
            check_vram(self.fake_torch(1.6), "cuda", root, "acestep-v15-turbo", "acestep-5Hz-lm-1.7B", logs.append)
        text = str(ctx.exception)
        self.assertIn("Not enough free GPU memory", text)
        self.assertIn("1.6 GB of 64 GB is free", text)
        self.assertIn("LM Studio", text)
        self.assertIn("needs about", logs[0])

    def test_enough_memory_and_non_gpu_pass(self):
        from engine_host.backends.acestep import check_vram, estimate_vram
        root = self.checkpoints()
        check_vram(self.fake_torch(40), "cuda", root, "acestep-v15-turbo", "acestep-5Hz-lm-1.7B", lambda s: None)
        check_vram(self.fake_torch(0), "cpu", root, "acestep-v15-turbo", None, lambda s: None)
        # fp32 on ROCm doubles the bf16 DiT/VAE/text encoder; the LM stays bf16.
        fp32 = estimate_vram(root, "acestep-v15-turbo", "acestep-5Hz-lm-1.7B", 4) - 3 * 2**30
        bf16 = estimate_vram(root, "acestep-v15-turbo", "acestep-5Hz-lm-1.7B", 2) - 3 * 2**30
        self.assertAlmostEqual(fp32 / 2**30, 0.7 * 2 + 0.2, delta=0.01)
        self.assertAlmostEqual(bf16 / 2**30, 0.9, delta=0.01)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class KeepAudioTests(unittest.TestCase):
    def test_failed_encoding_keeps_wav_and_retry_encodes_it(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        broken = os.path.join(tmp, "ffmpeg")
        with open(broken, "w") as f:
            f.write("#!/bin/sh\necho 'broken encoder' >&2\nexit 1\n")
        os.chmod(broken, 0o755)
        out = os.path.join(tmp, "Audio", "song.m4a")
        e = EngineProcess(["--backend", "mock"], env={"MUSIC_ENGINE_FFMPEG": broken})
        try:
            e.send({"load": {"modelPath": ROOT}})
            e.read_until("ready")
            e.send(job(out))
            failed = e.read_until("failed")["failed"]
            self.assertIn("recording was kept", failed["message"])
        finally:
            e.close()
        kept = os.path.join(tmp, "Audio", "song.unencoded.wav")
        self.assertTrue(os.path.exists(kept))
        e = EngineProcess(["--backend", "mock"], env={"MUSIC_ENGINE_FFMPEG": FFMPEG})
        try:
            e.send(job(out, jid="t2"))
            done = e.read_until("finished")["finished"]["_0"]
            self.assertEqual(done["id"], "t2")
            stages = {m["progress"]["_1"]["stage"] for m in e.lines if "progress" in m}
            self.assertEqual(stages, {"encoding"})  # encoded, not recorded again
            self.assertTrue(os.path.exists(out))
            self.assertFalse(os.path.exists(kept))
        finally:
            e.close()

    def test_missing_configured_ffmpeg_falls_back_to_path(self):
        self.assertEqual(audio.find_ffmpeg("/nope/ffmpeg"), FFMPEG)
