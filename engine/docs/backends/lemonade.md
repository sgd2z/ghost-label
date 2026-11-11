# Lemonade Server backend (`--backend lemonade`)

Records songs with **AMD Lemonade Server**'s `ACE-Step-Music` model instead of running ACE-Step in
the engine's own Python. Lemonade serves ACE-Step 1.5 through acestep.cpp (`ace-server`), on
Vulkan, ROCm (gfx1151 included) or CUDA.

## Setup

1. Install Lemonade Server (Windows: `https://github.com/lemonade-sdk/lemonade/releases/latest/download/lemonade.msi`) and start it.
2. Download the music model once (about 10.5 GB):

   ```
   lemonade pull ACE-Step-Music
   ```

3. In Ghost Label: **Settings › Music engine › Engine › Backend: Lemonade Server (ACE-Step-Music)**.
   The Lemonade URL defaults to the AI provider's base URL (the Lemonade preset uses
   `http://localhost:13305/v1`); set it when Lemonade isn't also your chat server. Press
   **Test music engine**, then **Save engine settings**.
4. No built-in engine yet? Install with Hardware **Lemonade Server only**: a small environment
   (Python, numpy, ffmpeg, yt-dlp), no PyTorch, no local model.

## What it sends

`POST {url}/audio/generations`:

| Lemonade field   | From the app                                                     |
|------------------|------------------------------------------------------------------|
| `model`          | `ACE-Step-Music` (Settings › Music model)                        |
| `prompt`         | the song's style caption with the artist's vocal tags            |
| `lyrics`         | the lyrics, or `[Instrumental]`                                  |
| `vocal_language` | the song's language, when it is one of ACE-Step's                |
| `duration`       | the song's target length (capped by Longest song)                |
| `steps`          | Quality: Demo 32, Balanced 48, Studio 64 (`steps_map` overrides) |
| `seed`           | the song's seed                                                  |
| `response_format`| `wav`                                                            |

Lemonade answers once with the whole song as a 24-bit WAV, which the host encodes to .m4a with
the same ffmpeg as the built-in engine. Errors come back as JSON and are shown with what to do
(`lemonade pull …` for a missing model, free GPU memory for a load failure or a crash).

## What it can't do (compared with the built-in engine)

- **No progress.** Lemonade reports nothing until the song is done, so the app shows the stage
  and a timer, never a percentage.
- **No cancel.** Stopping a song stops waiting for it; Lemonade still finishes it in the background.
- **No reference audio, bpm or key.** Lemonade doesn't forward them to ace-server. Songs with
  reference audio (mashup takes, voice mode, "keep each artist's voice") go to the built-in
  engine when it's installed (Lemonade's music model is unloaded first); otherwise they are
  recorded by Lemonade without the reference, and the song says so.
- **No audio analysis.** Listening to linked audio (audio/voice/mashup modes) uses the built-in engine.

## Memory

Lemonade loads the music model on the first song (nothing is loaded by the Test button or the
engine start). With **Unload image model before recording** (AI provider) and **Unload the chat
model in Lemonade before each song** (Music engine), Lemonade frees those models' memory first
(`POST /v1/unload`). The built-in engine is stopped before a Lemonade song, and Lemonade's music
model is unloaded before a built-in one.

## Options

`url` (`$LEMONADE_URL`), `model` (`$LEMONADE_MUSIC_MODEL`), `api_key` (`$LEMONADE_API_KEY`, only
when the server sets one; the app passes the key saved for that same server), `timeout` (seconds
per song, default 3600), `steps_map`.
