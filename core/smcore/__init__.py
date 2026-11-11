"""Ghost Label core: the Python sidecar behind the Electron UI.

Everything that isn't drawing pixels lives here: settings and secrets, the AI provider client,
the library database, library building, taste, links analysis, the studio queue that drives the
engine process (../engine), sharing and export. Stdlib only; `keyring` and `yt_dlp` are optional.
"""

__version__ = "0.1.0"
