"""python -m smcore [--port N] [--home DIR] [--ui DIR] [--engine-dir DIR] [--uv PATH] [--token T]

Prints one line `SMCORE READY {"port": ..., "token": ...}` on stdout once listening."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smcore.app import App  # noqa: E402
from smcore.server import serve  # noqa: E402


def main():
    # Log lines carry lyrics in any script: never let the console's code page mangle or crash them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--home")
    p.add_argument("--ui")
    p.add_argument("--engine-dir")
    p.add_argument("--uv")
    p.add_argument("--token")
    args = p.parse_args()
    from smcore.paths import Paths
    from smcore.procs import hold_core_lock
    say = lambda t: print(t, file=sys.stderr, flush=True)  # noqa: E731
    home = Paths(args.home).home
    hold_core_lock(home, log=say)
    from smcore.paths import legacy_homes
    from smcore.procs import kill_stale_children
    kill_stale_children([os.path.join(h, "Engine") for h in [home, *legacy_homes()]], log=say)
    app = App(home=args.home, engine_dir=args.engine_dir, uv=args.uv)
    server, token = serve(app, port=args.port, token=args.token or os.environ.get("SM_TOKEN"), ui_dir=args.ui)
    print("SMCORE READY " + json.dumps({"port": server.server_address[1], "token": token, "home": app.paths.home}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.engine.stop()
        app.builtin_engine.stop()


if __name__ == "__main__":
    main()
