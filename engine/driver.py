#!/usr/bin/env python3
"""A stand-in for Standard Music: launches an engine the way the app does, records one song, and
plays it. Works with any engine that speaks docs/custom-engines.md, not just this repo's.

    python driver.py                                   # engine.py beside this file, default backend
    python driver.py --engine-arg=--backend --engine-arg=mock --no-play
    python driver.py --engine /path/to/other_engine.py --model some/model

What it mirrors from the app (Packages/StandardKit/.../EngineClient.swift): the program runs
with its own folder as the working directory, MLX_ENABLE_TF32=0 in the environment, stdin and
stdout as JSON lines, `load` (Model, or the program's folder when Model is empty) then
`generate`, `cancel` while rendering, `shutdown` at the end, and a restart if the engine is silent
for ten minutes mid-song.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_STYLE = ("English, indie rock, driving, crunchy electric guitars, female vocal, powerful, "
                "warm alto")
SAMPLE_LYRICS = """[Intro]

[Verse]
Headlights on the county road,
Radio low and the night is cold,
Every mile a little more alive,
Window down and we're doing fine.

[Chorus]
So turn it up, turn it up,
Let the whole town hear us now,
Turn it up, turn it up,
We were never meant to slow down.

[Outro]
"""
STALL_SECONDS = 600


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--engine", default=os.path.join(HERE, "engine.py"),
                   help="engine program (default: engine.py beside this script)")
    p.add_argument("--python", default=None,
                   help="interpreter for a .py engine (default: this python; ignored for executables)")
    p.add_argument("--engine-arg", action="append", default=[],
                   help="argument passed to the engine, repeatable (like the app's Arguments field)")
    p.add_argument("--model", default="", help="the app's Model field (default: empty)")
    p.add_argument("--style", default=SAMPLE_STYLE)
    p.add_argument("--lyrics", default=None, help="lyrics text (default: a short sample song)")
    p.add_argument("--lyrics-file", default=None, help="read lyrics from a UTF-8 file")
    p.add_argument("--instrumental", action="store_true")
    p.add_argument("--seed", type=int, default=1216756351)
    p.add_argument("--steps", type=int, default=16, choices=[8, 16, 32],
                   help="the app's quality setting: 8 Demo, 16 Balanced, 32 Studio")
    p.add_argument("--max-seconds", type=float, default=60)
    p.add_argument("--title", default="Turn It Up")
    p.add_argument("--artist", default="The Halts")
    p.add_argument("--voice", default="female vocal, powerful, warm alto, slightly husky")
    p.add_argument("--planning", default="full", choices=["off", "melody", "full"])
    p.add_argument("--out", default=None, help="output .m4a (default: ./out/<id>.m4a)")
    p.add_argument("--load-timeout", type=float, default=0,
                   help="seconds to wait for ready (default 0 = forever; the first load may download models)")
    p.add_argument("--cancel-after", type=float, default=None,
                   help="send cancel this many seconds into the song (tests cancellation)")
    p.add_argument("--songs", type=int, default=1, help="record this many songs in one engine session")
    p.add_argument("--play", default="auto",
                   help="auto (OS default player, else ffplay), ffplay, none, or a player command")
    p.add_argument("--no-play", action="store_const", const="none", dest="play")
    p.add_argument("--engine-log", default=None, help="write the engine's stderr here instead of the console")
    p.add_argument("--json", action="store_true", help="print every protocol line received")
    return p.parse_args()


def engine_command(args):
    engine = os.path.abspath(os.path.expanduser(args.engine))
    if not os.path.exists(engine):
        sys.exit(f"engine not found: {engine}")
    if engine.lower().endswith(".py"):
        return [args.python or sys.executable, engine] + args.engine_arg, engine
    return [engine] + args.engine_arg, engine


class Engine:
    def __init__(self, command, program, log_file=None, echo=False):
        env = dict(os.environ)
        env["MLX_ENABLE_TF32"] = "0"
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self.stderr = open(log_file, "a", encoding="utf-8") if log_file else None
        self.proc = subprocess.Popen(
            command, cwd=os.path.dirname(program), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, bufsize=0)
        self.events = queue.Queue()
        self.echo = echo
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if self.echo:
                print("<< " + line, file=sys.stderr)
            try:
                self.events.put(json.loads(line))
            except json.JSONDecodeError:
                print(f"\n[driver] engine wrote a non-JSON line to stdout: {line[:200]}", file=sys.stderr)
        self.events.put(None)  # EOF

    def send(self, message):
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def next_event(self, timeout):
        return self.events.get(timeout=timeout)

    def close(self):
        if self.proc.poll() is None:
            try:
                self.send({"shutdown": {}})
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        if self.stderr:
            self.stderr.close()


def bar(p):
    width = 30
    filled = int(round(p["overall"] * width))
    song = f" ~{p['estimatedSongSeconds']:.0f}s song" if p.get("estimatedSongSeconds") else ""
    return (f"\r[{'#' * filled}{'.' * (width - filled)}] {p['overall'] * 100:5.1f}%  "
            f"{p['stage']:<9} {p['stageFraction'] * 100:5.1f}%  {p['elapsed']:6.1f}s{song}   ")


def wait_ready(engine, timeout):
    start = time.time()
    while True:
        remaining = None if not timeout else max(0.1, timeout - (time.time() - start))
        try:
            event = engine.next_event(remaining if remaining is not None else 1.0)
        except queue.Empty:
            if timeout and time.time() - start >= timeout:
                raise SystemExit("[driver] the engine didn't report that it was ready")
            continue
        if event is None:
            raise SystemExit(f"[driver] engine exited during load (code {engine.proc.wait()})")
        if "ready" in event:
            return event["ready"].get("loadSeconds", 0)
        if "failed" in event:
            raise SystemExit("[driver] load failed: " + event["failed"].get("message", "?"))


def record(engine, job, cancel_after=None):
    start = last = time.time()
    cancel_sent = False
    while True:
        if cancel_after is not None and not cancel_sent and time.time() - start >= cancel_after:
            engine.send({"cancel": {"id": job["id"]}})
            cancel_sent = True
            print(f"\n[driver] sent cancel after {cancel_after}s", file=sys.stderr)
        try:
            event = engine.next_event(0.2)
        except queue.Empty:
            if time.time() - last > STALL_SECONDS:
                engine.proc.terminate()
                raise SystemExit("[driver] engine silent for ten minutes mid-song; stopped it (the app restarts it)")
            continue
        last = time.time()
        if event is None:
            raise SystemExit(f"\n[driver] engine exited mid-song (code {engine.proc.wait()})")
        if "progress" in event and event["progress"].get("id") == job["id"]:
            sys.stderr.write(bar(event["progress"]["_1"]))
            sys.stderr.flush()
        elif "finished" in event and event["finished"]["_0"].get("id") == job["id"]:
            sys.stderr.write("\n")
            return "finished", event["finished"]["_0"]
        elif "failed" in event and event["failed"].get("id") in (None, job["id"]):
            sys.stderr.write("\n")
            return "failed", event["failed"]
        elif "cancelled" in event and event["cancelled"].get("id") == job["id"]:
            sys.stderr.write("\n")
            return "cancelled", event["cancelled"]
        elif "log" in event:
            print(f"\n[engine log] {event['log']}", file=sys.stderr)


def probe(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                          "format=format_name,duration:stream=codec_name,sample_rate,channels",
                          "-of", "json", path], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 else None


def play(path, how):
    if how == "none":
        return
    ffplay = shutil.which("ffplay")
    if how == "ffplay" or (how == "auto" and ffplay and os.environ.get("MUSIC_ENGINE_PLAYER") == "ffplay"):
        if not ffplay:
            print("[driver] ffplay not found", file=sys.stderr)
            return
        subprocess.run([ffplay, "-nodisp", "-autoexit", "-loglevel", "error", path])
        return
    if how != "auto":  # a player command, e.g. "vlc" or "mpv --no-video"
        import shlex
        subprocess.Popen(shlex.split(how, posix=os.name != "nt") + [path])
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 — the default app for .m4a
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=True)
        else:
            subprocess.run(["xdg-open", path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[driver] opened {path} in the default player", file=sys.stderr)
    except (OSError, subprocess.CalledProcessError) as error:
        if ffplay:
            subprocess.run([ffplay, "-nodisp", "-autoexit", "-loglevel", "error", path])
        else:
            print(f"[driver] couldn't open a player ({error}); the file is at {path}", file=sys.stderr)


def main():
    args = parse_args()
    lyrics = args.lyrics if args.lyrics is not None else SAMPLE_LYRICS
    if args.lyrics_file:
        with open(args.lyrics_file, encoding="utf-8") as f:
            lyrics = f.read()
    command, program = engine_command(args)
    print("[driver] launching: " + " ".join(command), file=sys.stderr)
    engine = Engine(command, program, args.engine_log, args.json)
    status = 1
    try:
        model = args.model or os.path.dirname(program)  # the app sends the program's folder when Model is empty
        engine.send({"load": {"modelPath": model}})
        print("[driver] waiting for ready (the first run may download model weights) ...", file=sys.stderr)
        load_seconds = wait_ready(engine, args.load_timeout)
        print(f"[driver] ready (load {load_seconds:.1f}s)", file=sys.stderr)
        for n in range(args.songs):
            jid = str(uuid.uuid4())
            out = os.path.abspath(args.out if args.out and args.songs == 1 else
                                  os.path.join(os.getcwd(), "out", f"{jid}.m4a"))
            job = {
                "id": jid, "style": args.style, "lyrics": lyrics, "planning": args.planning,
                "seed": args.seed + n, "steps": args.steps, "maxSemanticTokens": 8250,
                "outputPath": out, "title": args.title, "artist": args.artist, "voice": args.voice,
                "isInstrumental": args.instrumental, "maxSeconds": args.max_seconds,
            }
            engine.send({"generate": {"_0": job}})
            outcome, payload = record(engine, job, args.cancel_after)
            if outcome == "finished":
                info = probe(out)
                print(f"[driver] finished: {payload['duration']:.1f}s song in {payload['seconds']:.1f}s "
                      f"({payload['duration'] / max(payload['seconds'], 1e-6):.2f}x realtime), "
                      f"loudness {payload['loudness']:.1f} dB, truncated={payload['truncated']}", file=sys.stderr)
                print(f"[driver] wrote {out}", file=sys.stderr)
                if info:
                    stream = (info.get("streams") or [{}])[0]
                    print(f"[driver] ffprobe: {stream.get('codec_name')} {stream.get('sample_rate')} Hz "
                          f"{stream.get('channels')} ch, {float(info['format']['duration']):.1f}s", file=sys.stderr)
                print(out)
                status = 0
                if n == args.songs - 1:
                    play(out, args.play)
            elif outcome == "cancelled":
                print("[driver] cancelled", file=sys.stderr)
                status = 0 if args.cancel_after is not None else 1
            else:
                print("[driver] failed: " + payload.get("message", "?"), file=sys.stderr)
                status = 1
    except KeyboardInterrupt:
        print("\n[driver] interrupted", file=sys.stderr)
    finally:
        engine.close()
    sys.exit(status)


if __name__ == "__main__":
    main()
