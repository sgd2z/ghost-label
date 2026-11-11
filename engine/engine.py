#!/usr/bin/env python3
"""Ghost Label engine host (speaks the standardagents/music custom-engine protocol).

Point Standard Music › Settings › Studio › Your own engine at this file (see README.md), or run it
through driver.py. It speaks the custom engine protocol (docs/custom-engines.md in
standardagents/music) on stdin/stdout and hands each song to a backend:

    python engine.py --backend acestep            # ACE-Step 1.5 (default)
    python engine.py --backend mock               # test tones, no model, for testing
    python engine.py --backend lemonade           # AMD Lemonade Server's ACE-Step-Music ($LEMONADE_URL)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine_host import native  # noqa: E402

native.apply_env()
for _stream in (sys.stderr,):  # stdout is the protocol; host.py sets it up
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass  # before numpy/scipy/torch load: OpenBLAS thread cap etc. on Windows

from engine_host.host import main  # noqa: E402  (stdlib only until stdout is claimed)

if __name__ == "__main__":
    main(__file__)
