import os
from pathlib import Path

DEFAULT_LM_MODEL = "acestep-5Hz-lm-1.7B"


def get_checkpoints_dir(custom_dir=None):
    if custom_dir:
        return Path(custom_dir)
    if os.environ.get("ACESTEP_CHECKPOINTS_DIR"):
        return Path(os.environ["ACESTEP_CHECKPOINTS_DIR"])
    return Path(os.environ.get("ACESTEP_PROJECT_ROOT", os.getcwd())) / "checkpoints"

SUBMODEL_REGISTRY = {"acestep-5Hz-lm-0.6B": "ACE-Step/acestep-5Hz-lm-0.6B",
                     "acestep-5Hz-lm-4B": "ACE-Step/acestep-5Hz-lm-4B"}


def check_model_exists(model_name, checkpoints_dir=None):
    return (Path(checkpoints_dir or get_checkpoints_dir()) / model_name).exists() or model_name == DEFAULT_LM_MODEL


def ensure_lm_model(model_name=None, checkpoints_dir=None, token=None, prefer_source=None):
    from acestep import record
    record("ensure_lm_model", model_name=model_name, checkpoints_dir=str(checkpoints_dir))
    return True, "downloaded"
