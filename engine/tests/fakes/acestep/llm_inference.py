from acestep import record


class LLMHandler:
    llm_initialized = False

    def initialize(self, checkpoint_dir, lm_model_path, backend="vllm", device="auto",
                   offload_to_cpu=False, dtype=None):
        record("llm_initialize", checkpoint_dir=checkpoint_dir, lm_model_path=lm_model_path,
               backend=backend, device=device)
        self.llm_initialized = True
        return "ok", True
