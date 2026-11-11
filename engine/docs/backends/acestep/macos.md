# ACE-Step 1.5 on macOS (Apple silicon)

This is the only platform where Standard Music itself can launch the engine.

```sh
git clone https://github.com/ace-step/ACE-Step-1.5 ~/music-engine/ACE-Step-1.5
cd ~/music-engine/ACE-Step-1.5 && uv sync
git clone https://github.com/sgd2z/ghost-label ~/music-engine/ghost-label
```

`uv sync` is ACE-Step's standard install. On Apple silicon it installs torch with MPS and MLX
([INSTALL.md](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md)). On
`mps`, the backend uses ACE-Step's `mlx` LM backend, as its `start_gradio_ui_macos.sh` does.
ffmpeg is optional on macOS: without it, the host encodes with the built-in
`/usr/bin/afconvert`, using the same settings as the app's reference engine.

Try it with the driver:

```sh
cd ~/music-engine/ghost-label/engine
python3 driver.py --python ~/music-engine/ACE-Step-1.5/.venv/bin/python \
  --engine-arg=--set --engine-arg=root=$HOME/music-engine/ACE-Step-1.5
```

Then register it in **Standard Music › Settings › Studio › Your own engine**:

- **Program:** `~/music-engine/ghost-label/engine/engine.py` (run `chmod +x` on it if needed)
- **Arguments:** `--python ~/music-engine/ACE-Step-1.5/.venv/bin/python --set root=~/music-engine/ACE-Step-1.5`
- **Model:** leave it empty, or enter a DiT name such as `acestep-v15-xl-turbo`

Run the driver once before you press **Test Engine**. The first load downloads about 10 GB, and
the app only waits 180 s for `ready`.

Options, mapping and model downloads: see [README.md](README.md). **(Not tested on a Mac for
this repo.)**
