"""Tiny HTTP helpers on urllib (no third-party deps)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

USER_AGENT = "GhostLabel/0.2 (+https://github.com/sgd2z/ghost-label)"


class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status, self.body, self.url = status, body, url


def _context():
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def request(method: str, url: str, body=None, headers=None, timeout: float = 60, raw: bool = False):
    data = None
    hdrs = {"User-Agent": USER_AGENT}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_context() if url.startswith("https") else None) as resp:
            content = resp.read()
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")
        raise HTTPError(error.code, text, url) from None
    if raw:
        return content
    text = content.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        raise HTTPError(200, "not JSON: " + text[:300], url) from None


def get_json(url: str, timeout: float = 20, headers=None):
    return request("GET", url, headers=headers, timeout=timeout)


def download(url: str, timeout: float = 60) -> bytes:
    return request("GET", url, timeout=timeout, raw=True)
