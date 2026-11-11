class _Config:
    max_duration_with_lm = 480
    max_duration_without_lm = 600


def get_gpu_config(gpu_memory_gb=None):
    return _Config()
