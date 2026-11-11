# Ghost Label

A cross-platform desktop music app. Your AI provider writes an endless library of artists who
don't exist: bios, voices, discographies, liner notes, lyrics and covers. A music model on your own
computer records each song as you play it.

- **Taste onboarding.** You react to real artists from a live catalog search (Deezer's public
  API), the AI asks a few adaptive questions, and then it writes your taste profile.
- **Library.** Artists, albums, songs, favorites and search. You can ask for "More like this",
  new releases, or a custom song written from your own style and lyrics.
- **From links.** Paste YouTube links or a playlist plus a note. Each track gets analysed in one of
  three modes: metadata (the default), audio (the music model listens), or voice (also used as
  reference audio). The AI then writes a versioned style brief, which you refine and turn into
  artists, an album or songs. Mashups and Hindi/Urdu are supported.
- **Studio.** Songs are recorded ahead of playback, with live progress, pause and cancel.
  The music engine is ACE-Step 1.5 in the app's own Python, or AMD Lemonade Server's
  `ACE-Step-Music` (`lemonade pull ACE-Step-Music`; see `engine/docs/backends/lemonade.md`).
- **Player.** A queue that can hold not-yet-recorded songs, loudness normalisation, and lyrics
  that follow along (time-shared by line length, since there's no timing data). There's an Up next
  panel and Media Session keys.
- **Sharing.** `.glalbum` share files, plus audio export (m4a with tags and cover).
- **Settings.** Any OpenAI-compatible provider, engine install and repair, playback options, and
  an editor for the rules the AI follows.

## Architecture

```
app/     Electron + TypeScript shell and UI (the renderer talks HTTP/SSE to the core)
core/    Python core "smcore" (stdlib only): library DB, AI calls, jobs, studio, links, sharing,
         installer, HTTP server that also serves the UI
engine/  Python engine host: one process per music model, JSON-lines protocol (ACE-Step 1.5, mock)
docs/    windows-quickstart.md
```

Electron starts the core with `python -m smcore`. The packaged app creates that Python
environment with a bundled `uv`. The core starts the engine as a separate process, so a GPU crash
never takes down the app.

**AI provider.** This is any OpenAI-compatible server: OpenAI, OpenRouter, LM Studio, Ollama,
llama.cpp, and others. You set a base URL, an optional key (stored in the OS keyring; the file
fallback is flagged as insecure), a main model and a fast model, and an optional images endpoint.
Replies come back as JSON from chat completions. The app tries strict json_schema first, then
json_object, then plain text, and it repairs and validates the reply mechanically. If images
aren't available, it falls back to generated placeholder art. There's a **Test connection**
button.

**No hard-coded taste.** Every creative judgment lives in plain-text rule files
(`core/smcore/rules/*.md`) that are sent to the AI with each request. You can edit them in
Settings › Rules. The code only does mechanical work: limits, validation, queues and files.

**Engine protocol.** See `engine/README.md`. The app's extensions are all optional:
- `ready.capabilities`;
- the generate fields `vocalLanguage`, `referenceAudio`, `bpm` and `keyscale`;
- an `analyze` command.

## Attribution and license

Ghost Label is based on [Standard Music](https://github.com/standardagents/music). It is a
cross-platform rewrite: an Electron/TypeScript app with a Python core that runs on Windows, Linux
and macOS, with local music engines including ACE-Step and Lemonade.

- It speaks Standard Music's custom engine protocol.
- Parts of the engine host were adapted from Standard Music's sample engine
  (`tools/engines/minimax_engine.py`).
- It is not affiliated with or endorsed by the Standard Music authors.

**License:** [Apache-2.0](LICENSE). Standard Music is MIT-licensed (Copyright (c) 2026 FormKit Inc.),
and its notice is kept in [NOTICE](NOTICE) along with credits for third-party components.

ACE-Step 1.5 (code and weights) is MIT-licensed and gets downloaded at install time. It isn't
vendored here.

## Run from source

```bash
cd app && npm install && npm start          # needs Node 20+ and Python 3.11/3.12 (or uv on PATH)
```

To try a demo with no AI and no GPU, run a fake OpenAI-compatible server, a fake catalog and the
mock engine, then attach Electron to them:

```bash
python core/tests/demo.py --seed            # prints DEMO READY http://127.0.0.1:8765/?token=demo
cd app && npm run build && SM_CORE_URL="http://127.0.0.1:8765/?token=demo" npx electron .
```

## Tests

```bash
cd engine && python -m unittest discover -s tests     # engine host and backends (fakes for ACE-Step)
cd core && python -m unittest discover -s tests       # core, including the end-to-end flow on fake servers
cd app && npm run typecheck && npm run build
```

## Builds

`npm run dist:win` builds a zip (`dist:win-portable` builds a single exe), `dist:linux` builds an
AppImage, and `dist:mac` builds a zip. Each build bundles the Electron app, the UI, the core and
engine sources, the docs, and `uv`. It does **not** bundle Python, PyTorch, ACE-Step, the models,
ffmpeg or yt-dlp. The app downloads those on first run or from Settings › Music engine.

Windows on an AMD Ryzen AI Max (gfx1151): see [docs/windows-quickstart.md](docs/windows-quickstart.md).
