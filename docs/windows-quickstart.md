# Windows quick start (Ryzen AI Max+ 395 / Radeon 8060S, gfx1151)

These are the exact steps for the Strix Halo PC. Anything marked **(unverified)** hasn't been
tried on real gfx1151 hardware yet; see the last section.

## 0. Before you start

- Windows 11 x64, the current **AMD Software: Adrenalin Edition** driver, an internet connection,
  and about **30 GB** of free disk space (PyTorch ROCm is about 4 GB and ACE-Step's models about 10 GB).
- **Variable Graphics Memory:** keep it small (0.5–1 GB in AMD Software). That way the GPU can
  borrow shared memory for both the music model and a local LLM. One user reported that a large
  carve-out capped HIP allocations; AMD couldn't reproduce it, so treat this as a workaround
  (details in `engine/docs/backends/acestep/windows-rocm.md`).
- Optional, for YouTube links: install a JS runtime for yt-dlp with `winget install DenoLand.Deno`.

## 1. Install the app

1. Unzip `Ghost Label-0.1.1-win.zip` to a folder you own, for example
   `C:\Apps\GhostLabel`. The zip is the recommended download.
2. Run `Ghost Label.exe`. The build is unsigned, so SmartScreen may warn: click
   **More info → Run anyway**.
3. First launch takes about a minute. The bundled `uv` downloads Python 3.12 and creates the app's
   core environment in `%APPDATA%\Ghost Label\core-venv`. Later launches are fast.

## 2. Connect an AI (onboarding step "AI provider")

Pick a preset, then press **Test connection**. A ✓ appears with the model's reply time and which
JSON mode works. Test also fills the model list.

**LM Studio** (simplest on Strix Halo):
1. Install LM Studio and download a model, for example *Qwen3 14B* or *Gemma 3 12B* (instruct).
   Choose the Vulkan or ROCm llama.cpp runtime in LM Studio's settings.
2. Go to **Developer → Start server**. It listens on `http://localhost:1234`.
3. In the app, choose **LM Studio**. The base URL is `http://localhost:1234/v1`, and you can leave
   the key empty. Press Test, then pick the model id from the list for both Main and Fast.

**Ollama:**
1. Install Ollama, then run `ollama pull qwen2.5:14b` (plus `qwen2.5:7b` for the Fast model if you like).
2. Make sure Ollama is running (tray icon, or `ollama serve`).
3. In the app, choose **Ollama**. The base URL is `http://localhost:11434/v1` and needs no key.
   Press Test. (Whether Ollama uses the gfx1151 GPU on Windows depends on its version: **unverified**.)

Cloud providers (OpenAI, OpenRouter) work the same way with a key. Keys are stored in Windows
Credential Manager through `keyring`. Local servers usually can't make images, so covers and
portraits become generated placeholder art. To get real art, turn on an images endpoint under
**Advanced**.

## 3. Install the music engine (onboarding step "Music engine")

1. Set Hardware to **AMD GPU on Windows (ROCm)**, ROCm wheels to **10.0.0**, and GPU arch to
   **gfx1151**. Leave "Download model weights now" ticked.
2. Click **Install engine**. You can keep going with onboarding while it works. The install runs
   in the background, with progress under Activity and the full log in Settings › Music engine ›
   Engine log. It:
   - creates `%APPDATA%\GhostLabel\Engine\venv` (Python 3.12, via the bundled uv);
   - installs `torch[device-gfx1151]==2.13.0+rocm10.0.0`, torchvision and torchaudio from
     `https://stable.repo.amd.com/rocm/whl-next/`;
   - downloads ACE-Step 1.5 (MIT) and installs its ROCm requirements, pinned so torch isn't
     replaced;
   - checks that torch sees a HIP device;
   - installs yt-dlp;
   - downloads ffmpeg if it isn't on PATH;
   - downloads the ACE-Step `main` models (about 10 GB, resumable);
   - saves the ROCm environment ACE-Step recommends (`ACESTEP_LM_BACKEND=pt`,
     `MIOPEN_FIND_MODE=FAST`, `TORCH_COMPILE_BACKEND=eager`, `TOKENIZERS_PARALLELISM=false`, plus
     `ACESTEP_ROCM_DTYPE=float32`; `HSA_OVERRIDE_GFX_VERSION` is deliberately not set).
   Large downloads show bytes and speed on the job card ("Downloading rocm_sdk_core…: 312 / 758
   MB (41%) · 4.2 MB/s"), and the card's **Output** shows the installer's last lines. Only one
   install runs at a time: clicking Install again shows the running one. If nothing moves for 90 s
   the card says "No progress for N s" with **Retry**; after 10 minutes the step is restarted on
   its own. Model weights are downloaded by the app itself, 8 connections per large file (one
   connection to Hugging Face's CDN is often capped far below a gigabit line), with total GB,
   speed and time left on the card. An optional **Hugging Face token** field (Settings › Music
   engine) is sent as `HF_TOKEN` if anonymous downloads are slow or refused. Finished wheels are kept in `%APPDATA%\GhostLabel\Engine\wheels` and an interrupted
   download is resumed, so Retry (or a restart of the app) doesn't download them again.
3. If the GPU check fails, click **Reinstall / repair** with ROCm **10.1.0**. To try the UI
   without a GPU, pick **Test tones (no AI model)**, which installs in about a minute.

**Or record with AMD Lemonade Server** (ACE-Step 1.5 XL through acestep.cpp, on ROCm or Vulkan):
1. In a terminal: `lemonade pull ACE-Step-Music` (about 10.5 GB).
2. Settings › Music engine › Engine › **Backend: Lemonade Server (ACE-Step-Music)**. Leave Lemonade
   URL empty if Lemonade is also your AI provider; otherwise enter `http://localhost:13305/v1`.
3. Press **Test music engine** (it checks the server and the model; it records nothing), then
   **Save engine settings**.
4. Without the built-in engine, install with Hardware **Lemonade Server only** (a minute). With
   it, mashups and voice references still go to the built-in engine, which can use reference
   audio; Lemonade can't. Details: `engine/docs/backends/lemonade.md`.

Alternative: `engine\scripts\setup-acestep-windows-rocm.ps1` does the same install by hand.
Afterwards, put its `venv\Scripts\python.exe` into Settings › Music engine › Engine Python.

## 4. Finish onboarding and listen

1. Pick genres, react to real artists (♥ / 👍 / ✕), answer the AI's two to four questions, add
   notes (for example "Hindi and Urdu welcome"), and press **Build my library**.
2. When an album appears, press **Play**. Each song is recorded the first time you play it, and
   the next two are recorded ahead (change this in the Studio). The first recording after a
   launch is slow, because it loads the model and MIOpen warms up.
3. Other things to try:
   - **From links**: paste YouTube links or a playlist plus a note. You get a style brief you can
     refine, and you can make artists, an album or songs from it.
   - **Create › Write one song**: write a song from your own style and lyrics.
   - **Favorites** and **More like this** steer what gets made next.
   - **Share**: export an artist as a `.glalbum` file.

## Where things live

`%APPDATA%\GhostLabel\` holds the library database, settings, Artwork, Audio, Rules
(your edited copies), Engine (venv, ACE-Step and models), and Logs (`core.log`, `engine.log`).
The Electron shell's own log is in `%APPDATA%\Ghost Label\logs\main.log`. To
uninstall, delete the app folder and both of those folders.

## Upgrading from earlier private builds (0.2.x / Standard Music Desktop)

0.1.1 shares `%APPDATA%\GhostLabel` with the earlier private 0.2.1 and 0.2.2 builds, so settings
and any model weights already in `%APPDATA%\GhostLabel\Engine\ACE-Step-1.5\checkpoints`
(finished or partly downloaded) are reused. 0.2.0 kept its data in `%APPDATA%\StandardMusicDesktop`.
On first start, 0.1.1 copies your settings (AI provider, models, preferences) and edited rules
from there into `%APPDATA%\GhostLabel`; API keys saved in Windows Credential Manager under the
old name are still found. The library and the engine are not copied. Install the engine again
from Settings › Music engine. Once 0.1.1 works, you can delete `%APPDATA%\StandardMusicDesktop`
and `%APPDATA%\Standard Music Desktop`.

Settings › AI provider says where it saved ("Saved to …\settings.json · key in WinVaultKeyring").
If Windows Credential Manager can't be used, the key goes to `secrets.json` in the data folder, and
the page warns you that the file isn't encrypted.

## Not yet verified on real gfx1151 hardware

These haven't been run on the PC yet:
- the ROCm wheel install;
- the HIP GPU check;
- ACE-Step generation and audio analysis speed and memory;
- Hindi and Urdu vocal quality;
- LM Studio or Ollama running side by side with ACE-Step in shared memory;
- yt-dlp's YouTube JS runtime;
- Credential Manager through keyring;
- the Windows build itself. It was built on Linux, so neither the exe nor the uv-on-Windows
  first-run path has been launched on Windows.
