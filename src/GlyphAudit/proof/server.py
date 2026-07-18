"""Static file server for the shipped proof web app.

`glyph-audit proof serve` invokes this. It boots a tiny stdlib
`http.server.ThreadingHTTPServer` that serves two overlaid roots:

  1. The packaged webapp `dist/` — HTML, JS, CSS, favicon.
  2. The project's build-output directory (`proof.output_dir` in
     `glyph-audit.toml`) — TTFs, `proof-config.json`, per-face manifests.

Requests hit (2) first, then fall back to (1). That way TTFs and manifests
override the packaged shell without needing a rebuild of the JS bundle.
No Node.js at runtime; the packaged `dist/` was prebuilt at wheel-build
time.

Design note: we deliberately use `http.server` from the stdlib rather than
Flask / Starlette / any other framework so the tool has zero web-server
dependencies. The one hazard — content-type sniffing for `.ttf` — is
handled explicitly below.
"""

from __future__ import annotations

import http.server
import mimetypes
import os
import socketserver
import sys
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Optional

# Explicit content types for file suffixes the stdlib guesses wrong /
# doesn't know about. Missing types cause the browser to refuse the font
# under strict same-origin policy on some macOS Safari versions.
_EXTRA_MIME_TYPES = {
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff":  "font/woff",
    ".woff2": "font/woff2",
    ".json": "application/json",
    ".js":   "application/javascript",
    ".mjs":  "application/javascript",
}


def _webapp_dist_dir() -> Path:
    """Return the path to the packaged webapp/dist/.

    Uses `importlib.resources` so it works when the package is installed
    from a wheel *and* when it's imported from an editable checkout.
    """
    # `files()` returns a Traversable for the package directory; combine
    # with the sub-path to `dist/`. Materialised to a real filesystem path
    # via `as_file` for use with `http.server.SimpleHTTPRequestHandler`.
    dist_ref = resources.files("GlyphAudit.proof.webapp").joinpath("dist")
    with resources.as_file(dist_ref) as p:
        # `as_file` yields a real path inside a `with` block; for our
        # process-long lifetime, keep the path (it stays valid because
        # `dist/` is a real directory in the installed package tree, not
        # a synthesised extract).
        return Path(p)


class _OverlayHandler(http.server.SimpleHTTPRequestHandler):
    """Serve requests from `output_dir` first, then fall back to `dist_dir`.

    The two roots are class attributes stamped on by `make_handler` at
    server-boot time. SimpleHTTPRequestHandler resolves paths against its
    `directory` attribute — we override `translate_path` so we can look up
    the file in `output_dir`, then fall through to `dist_dir` if nothing
    matches.
    """
    output_dir: str = ""
    dist_dir: str = ""

    # Silence the default access-log spam so `glyph-audit proof serve`
    # doesn't drown out useful build output when the watcher and server
    # share a terminal.
    def log_message(self, fmt, *args):
        pass

    def translate_path(self, path):
        # Strip query strings / fragments the same way stdlib does.
        clean = super().translate_path(path)
        # `translate_path` resolved against `self.directory`, which we
        # left pointing at `dist_dir`. Try `output_dir` first by rewriting.
        rel = os.path.relpath(clean, self.dist_dir)
        candidate = os.path.normpath(os.path.join(self.output_dir, rel))
        # Reject any path escape attempts (e.g. `../../etc/passwd`); both
        # roots must contain the resolved candidate.
        if os.path.commonpath([candidate, self.output_dir]) == self.output_dir \
                and os.path.exists(candidate) and os.path.isfile(candidate):
            return candidate
        return clean


def make_handler(dist_dir: str, output_dir: str):
    """Factory: bake the two roots into a fresh handler subclass so we can
    hand it directly to `ThreadingHTTPServer` without carrying state
    through the request lifecycle.
    """
    for ext, mime in _EXTRA_MIME_TYPES.items():
        mimetypes.add_type(mime, ext)

    class Handler(_OverlayHandler):
        pass
    Handler.output_dir = os.path.abspath(output_dir)
    Handler.dist_dir = os.path.abspath(dist_dir)

    def initializer(*args, **kwargs):
        # Pin the handler's `directory` to `dist_dir` so
        # SimpleHTTPRequestHandler.translate_path has a sane default; our
        # override then tries output_dir first.
        return Handler(*args, directory=os.path.abspath(dist_dir), **kwargs)

    return initializer


def serve(
    output_dir: str,
    host: str = "127.0.0.1",
    port: int = 5173,
    open_browser: bool = True,
    dist_dir: Optional[str] = None,
) -> None:
    """Serve the proof web app at `http://{host}:{port}`.

    `output_dir` is the directory the build library wrote TTFs +
    manifests to. `dist_dir` defaults to the packaged webapp bundle;
    pass an override during webapp-development to serve a locally-built
    `dist/` instead.

    Blocks until KeyboardInterrupt. Reuses port 5173 by default so the
    URL matches Vite's dev-server default — anyone with a browser tab
    open at that URL won't need to update it after switching from
    dev-mode to `glyph-audit proof serve`.
    """
    dist = dist_dir or str(_webapp_dist_dir())
    if not os.path.isdir(dist):
        print(f"Error: packaged webapp dist not found at {dist}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(output_dir):
        print(f"Warning: output_dir {output_dir} does not exist yet — "
              "run `glyph-audit proof build` first.", file=sys.stderr)

    handler_factory = make_handler(dist, output_dir)

    class ReuseServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with ReuseServer((host, port), handler_factory) as httpd:
        url = f"http://{host}:{port}/"
        print(f"Serving proof app at {url}")
        print(f"  dist:       {dist}")
        print(f"  output_dir: {output_dir}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping.")
