"""A stub of the parts of ACE-Step 1.5 the backend calls, with the same signatures
(acestep/handler.py, llm_inference.py, inference.py, model_downloader.py, constants.py,
gpu_config.py). It records every call to $FAKE_ACESTEP_LOG as JSON lines and returns a sine wave."""
import json
import os


def record(event, **data):
    path = os.environ.get("FAKE_ACESTEP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, **data}, default=str) + "\n")
