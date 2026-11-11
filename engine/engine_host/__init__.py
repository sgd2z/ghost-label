"""A model-agnostic engine host for Ghost Label, compatible with the standardagents/music custom-engine protocol.

`engine.py` speaks the custom engine protocol from standardagents/music (docs/custom-engines.md)
on stdin/stdout. A *backend* (see `engine_host.backends`) turns one song request into audio
samples; the host does everything else: protocol, progress, cancellation, trimming, fades,
loudness and AAC/.m4a encoding.
"""

__version__ = "0.1.1"
