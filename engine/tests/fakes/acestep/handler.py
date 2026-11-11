import os

from acestep import record


class _FakeVAE:
    """Stands in for the VAE: decode() per tiled chunk, parameters() for the device."""

    def parameters(self):
        class P:
            class device:
                type = "cuda"
        return iter([P()])

    def decode(self, latents):
        import time
        time.sleep(float(os.environ.get("FAKE_ACESTEP_DECODE_DELAY", "0.3")))
        return latents


class AceStepHandler:
    def __init__(self):
        self.vae = _FakeVAE()

    def _tiled_decode_gpu(self, latents, stride, overlap, num_steps):
        for _ in range(num_steps):
            self.vae.decode(latents)
        return latents
    def is_flash_attention_available(self, device=None):
        return False

    def initialize_service(self, project_root, config_path, device="auto", use_flash_attention=False,
                           compile_model=False, offload_to_cpu=False, offload_dit_to_cpu=False,
                           quantization=None, prefer_source=None, use_mlx_dit=True, vae_checkpoint=None):
        record("initialize_service", project_root=project_root, config_path=config_path, device=device,
               offload_to_cpu=offload_to_cpu, checkpoints=os.environ.get("ACESTEP_CHECKPOINTS_DIR"))
        if config_path == "broken-model":
            return "ERROR: Failed to download DiT model 'broken-model'", False
        return "ok", True

    def convert_src_audio_to_codes(self, audio_file):
        record("convert_src_audio_to_codes", audio_file=audio_file)
        if not os.path.exists(audio_file):
            return "\u274c no such file"
        return "<|audio_code_1|><|audio_code_2|>"
