# engine: the model host

The model-agnostic **engine host** behind Ghost Label. It is also compatible with [Standard Music](https://github.com/standardagents/music):
it speaks Standard Music's custom engine protocol
([`docs/custom-engines.md`](https://github.com/standardagents/music/blob/main/docs/custom-engines.md))
on stdin/stdout, and hands each song to a pluggable **backend** that runs one music model.
The first backend is [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5). A `mock`
backend makes test tones for tests and for checking a setup.

```
Standard Music (or driver.py)  ──JSON lines──▶  engine.py  ──▶  backend.generate(request)
                               ◀─ ready/progress/finished ─      │  returns samples + sample rate
                                                                 ▼
                                     trim · cut + fade at maxSeconds · loudness · ffmpeg → AAC .m4a
```

The host handles the protocol, progress heartbeats, cancellation, leading-silence trim, fade-out,
loudness (the same measure as the app's reference engine) and atomic 256 kbps AAC `.m4a` writing.
A backend only turns a song request into audio.

> **Platforms.** The engine and driver run on Windows, Linux and macOS. **Standard Music itself is
> a macOS app** (Apple silicon, macOS 26), so only a Mac can launch the engine from the app's
> Settings. On Windows and Linux, `driver.py` stands in for the app: it launches the engine the
> same way the app does, records a song and plays it.

## Layout

| Path | What it is |
|---|---|
| `engine.py` | The engine program. Point Standard Music at this, or run it through `driver.py`. |
| `driver.py` | Stand-in for the app. Launches any protocol engine, sends `load` + `generate`, shows progress, plays the `.m4a`. |
| `engine_host/host.py` | Protocol loop, progress and heartbeat, cancel, config, and backend selection. |
| `engine_host/audio.py` | Trim, fade, loudness, and ffmpeg AAC/.m4a encoding (`afconvert` fallback on macOS). |
| `engine_host/devices.py` | PyTorch device choice: `cuda` (NVIDIA or AMD ROCm), then `mps`, `xpu`, `cpu`. Override with `MUSIC_ENGINE_DEVICE`. |
| `engine_host/backends/__init__.py` | The backend interface (`Backend`, `Capabilities`, `SongRequest`, `Audio`) and registry. |
| `engine_host/backends/acestep.py` | ACE-Step 1.5 backend. |
| `engine_host/backends/mock.py` | Test-tone backend. |
| `engine_host/backends/lemonade.py` | AMD Lemonade Server's `ACE-Step-Music` over HTTP ([docs](docs/backends/lemonade.md)). |
| `docs/backends/acestep/` | ACE-Step setup: **[Windows + AMD ROCm (gfx1151)](docs/backends/acestep/windows-rocm.md)**, [Linux](docs/backends/acestep/linux.md), [macOS](docs/backends/acestep/macos.md), and the [parameter mapping](docs/backends/acestep/README.md). |
| `scripts/setup-acestep-windows-rocm.ps1` | Automates the Windows ROCm guide. |
| `tests/` | Protocol, driver, audio and ACE-Step mapping tests (stub ACE-Step in `tests/fakes`). |
| `engine.example.json` | Example config file. |

## Quick start

You need Python 3.10 or newer with `numpy` and `soundfile` (`pip install -r requirements.txt`),
plus `ffmpeg` on PATH (`winget install -e --id Gyan.FFmpeg`, `brew install ffmpeg`, or your
distro's package).

Check the plumbing with the mock backend. No model is needed:

```sh
python driver.py --engine-arg=--backend --engine-arg=mock
```

You should see `ready`, a progress bar, then `finished`, and the song opens in your default
player. Then set up a real model:

- **ACE-Step 1.5 on Windows with an AMD Ryzen AI Max+ / Radeon 8060S (gfx1151):**
  [docs/backends/acestep/windows-rocm.md](docs/backends/acestep/windows-rocm.md)
- ACE-Step 1.5 on [Linux](docs/backends/acestep/linux.md) or [macOS](docs/backends/acestep/macos.md)

```sh
python driver.py --python <venv python> --engine-arg=--set --engine-arg=root=<ACE-Step-1.5 clone>
```

## Choosing the backend and its options

Each setting is taken from the first of these that sets it:

1. **Command line**: `--backend NAME`, and `--set key=value` (repeatable) for backend options.
2. **Environment**: `MUSIC_ENGINE_BACKEND`, plus each backend's own variables (listed in its docs).
3. **Config file**: `--config FILE`, else `$MUSIC_ENGINE_CONFIG`, else `engine.json` next to
   `engine.py`. See `engine.example.json`. Its `env` block fills in environment variables that
   aren't already set, and `options` holds backend options.
4. The default backend is `acestep`.

Host options: `--python PATH` re-runs the engine under another interpreter (a venv), so the `#!`
line can stay generic. `--ffmpeg PATH` or `MUSIC_ENGINE_FFMPEG` sets the encoder.
`--bitrate` defaults to 256000. `--list-backends` lists the backends.

## Using it from Standard Music (macOS)

In **Settings › Studio › Music engine › Your own engine**
([docs/custom-engines.md › Setting it up in the app](https://github.com/standardagents/music/blob/main/docs/custom-engines.md#setting-it-up-in-the-app)):

- **Program**: `/path/to/ghost-label/engine/engine.py`. It already has a `#!/usr/bin/env python3`
  line. Run `chmod +x engine.py` if your checkout lost the executable bit.
- **Arguments**: `--python /path/to/ACE-Step-1.5/.venv/bin/python --backend acestep --set root=/path/to/ACE-Step-1.5`
- **Model** (optional): an ACE-Step DiT name (`acestep-v15-turbo`, `acestep-v15-xl-turbo`,
  `acestep-v15-base`, …), a DiT folder, or a checkpoints folder. Leave it empty to use the
  default. The app then sends the engine's own folder, which the host treats as "no model given".

Press **Test Engine**. The first load downloads about 10 GB of ACE-Step weights, and the app
waits up to 180 s for `ready`, so it can time out. Run `driver.py` once first, or download the
models in advance (see the backend docs).

## driver.py

```
python driver.py [--engine engine.py] [--python PY] [--engine-arg ARG ...] [--model M]
                 [--style S] [--lyrics L | --lyrics-file F] [--instrumental] [--seed N]
                 [--steps 8|16|32] [--max-seconds S] [--out song.m4a] [--songs N]
                 [--cancel-after SECONDS] [--play auto|ffplay|none|"<player cmd>"] [--no-play]
                 [--engine-log FILE] [--json]
```

It mirrors what the app's `EngineClient` does. The engine runs with its own folder as the
working directory and `MLX_ENABLE_TF32=0` set. The driver sends `load` (Model, or the engine's
folder when Model is empty), then `generate`, sends `cancel` while a song renders, and sends
`shutdown` at the end. It gives up on an engine that stays silent for ten minutes mid-song.
To play the song it uses the OS default player (`os.startfile` on Windows, `open` on macOS,
`xdg-open` on Linux) and falls back to `ffplay`. It prints the output path on stdout. Logs go to
stderr.

## How a song request reaches a backend

The host turns the app's `generate` job into a `SongRequest`:

| App field | SongRequest | Notes |
|---|---|---|
| `id` | `id` | Echoed in every reply. |
| `style` | `style` | The main prompt (comma-separated tags, voice included). |
| `lyrics` | `lyrics` | Passed through with section tags. Dropped if the backend has `supports_lyrics=False`. |
| `isInstrumental` | `instrumental` | |
| `seed` | `seed` | `seed % 2**31`, so the same song comes out the same. |
| `steps` (8/16/32) | `quality`, `steps` | `steps = backend.map_steps(quality)`. Override with `--set steps_map=8:..,16:..,32:..`. |
| `maxSeconds` | `max_seconds` | `min(maxSeconds, Capabilities.max_seconds)`. Longer audio is cut there with a 3 s fade, and `truncated` is set. |
| `planning` | `planning` | YuE2's planner setting. Backends may map it. |
| `title`, `artist`, `voice` | same | For models that take them separately. |
| `maxSemanticTokens` | — | YuE2-only. Ignored. Still available in `request.raw`. |

Replies follow the protocol exactly: `ready{loadSeconds}`, `progress{id,_1:{stage,stageFraction,overall,elapsed,estimatedSongSeconds}}`,
`finished{_0:{id,duration,loudness,seconds,truncated}}`, `failed{id?,message}` and `cancelled{id}`.

## Adding a backend

1. Create `engine_host/backends/<name>.py`:

   ```python
   from engine_host.backends import Audio, Backend, Cancelled, Capabilities

   class MyModelBackend(Backend):
       name = "mymodel"
       steps_map = {8: 10, 16: 25, 32: 50}          # app quality -> your model's steps

       def capabilities(self):
           return Capabilities(max_seconds=240, supports_lyrics=True,
                               cooperative_cancel=True, reports_progress=True)

       def load(self, model_path):                   # None when the app's Model field is empty
           import mymodel                            # import heavy things here, not at the top
           self.model = mymodel.load(model_path or self.option("model", "org/default", ["MYMODEL_MODEL"]))

       def generate(self, request, progress, cancelled):
           def on_step(i, n):
               if cancelled():
                   raise Cancelled()
               progress("composing", i / n)          # stage, stage fraction [, overall 0..1]
           wav, sr = self.model.generate(text=request.style, lyrics=request.lyrics,
                                         seconds=request.max_seconds, steps=request.steps,
                                         seed=request.seed, callback=on_step)
           return Audio(wav, sr)                     # [frames], [frames, ch] or [ch, frames]
   ```

2. Register it in `BACKENDS` in `engine_host/backends/__init__.py`:
   `"mymodel": "engine_host.backends.mymodel:MyModelBackend"`. A backend outside this repo can
   also be named directly, as in `--backend mypackage.module:Class`.
3. Write `docs/backends/<name>/` with install steps for each platform, and list its options and
   environment variables.
4. Add tests. Stub the model's API the way `tests/fakes/acestep` stubs ACE-Step, so the
   parameter mapping is tested without the weights.

Rules worth keeping:
- Never write to stdout. The host routes stray stdout, even from C libraries, to stderr.
- Don't encode, trim or fade yourself. Return raw samples.
- If your model reports no progress, set `reports_progress=False`. The host then estimates
  progress from a learned speed and still sends a heartbeat every second.
- Keep the mapping mechanical. Translate the request onto the model's parameters, and leave
  anything else to options.

## Tests

```sh
python -m unittest discover -s tests -v
```

These launch `engine.py` as a subprocess, the same way the app does, for the mock backend and for
the real ACE-Step backend module against a stub of ACE-Step's API. They check every protocol
message, cancel before and during a song, failures, load errors, config files, the driver round
trip, the `.m4a` (with `ffprobe`) and the ACE-Step parameter mapping. They need `numpy` and
`ffmpeg`, but not PyTorch: a tiny torch stand-in is used when torch isn't installed.

## Licenses

This host has no license file yet; add one before you share it. ACE-Step 1.5 code and weights are
MIT. Check each model's license before you share what it makes.

## Protocol extensions (used by the desktop app; ignored by the Mac app)

All optional. An engine that doesn't implement them still works with the app, which then falls back.

- `ready` carries `capabilities`: `{"maxSeconds", "lyrics", "analyze", "referenceAudio", "languages"}`.
- `generate` jobs may carry `vocalLanguage` (ISO code, e.g. `hi`, `ur`), `referenceAudio` (path to a
  clip used for style/timbre guidance; ACE-Step `reference_audio`), `bpm` and `keyscale`.
- `{"analyze": {"_0": {"id": "...", "audioPath": "..."}}}` answers
  `{"analyzed": {"_0": {"id": "...", "seconds": 1.2, "result": {...}}}}` or `{"failed": {"id", "message"}}`.
  ACE-Step: audio -> semantic codes -> `understand_music` (caption, lyrics, bpm, keyscale, language,
  timesignature, duration); needs the LM planner. Mock: measured duration and RMS.
