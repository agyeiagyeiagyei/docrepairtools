"""Unit + integration tests for GlyphAudit.proof.server.

`_webapp_dist_dir` reads through `importlib.resources` so we can't easily
mock it — the tests below use a temp `dist_dir` explicitly instead. Real-
wheel behaviour (the packaged dist getting located after `pip install`)
is exercised by the L5 manual smoke checklist.
"""

from __future__ import annotations

import http.client
import os
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from GlyphAudit.proof import server


# ---------------------------------------------------------------------------
# Unit: overlay path resolution
# ---------------------------------------------------------------------------

class TestOverlayResolution:
    """Test `_OverlayHandler.translate_path` in isolation — the interesting
    logic doesn't need a live socket.
    """

    def _handler(self, dist_dir, output_dir):
        # Grab the class the factory builds without instantiating the socket
        # side. We can then call translate_path directly against a stub
        # request context.
        factory = server.make_handler(str(dist_dir), str(output_dir))
        cls = factory.__closure__[0].cell_contents \
            if hasattr(factory, "__closure__") and factory.__closure__ else None
        # `make_handler`'s inner closure is what carries the class; getting
        # it back reliably means we instantiate a subclass ourselves.
        class Stub(server._OverlayHandler):
            pass
        Stub.dist_dir = os.path.abspath(str(dist_dir))
        Stub.output_dir = os.path.abspath(str(output_dir))
        # translate_path only needs `self.directory` — mimic what
        # SimpleHTTPRequestHandler would have set.
        stub = Stub.__new__(Stub)
        stub.directory = os.path.abspath(str(dist_dir))
        stub.headers = {}
        return stub

    def test_hits_output_dir_first(self, tmp_path):
        dist   = tmp_path / "dist";   dist.mkdir()
        output = tmp_path / "output"; output.mkdir()
        (dist   / "hello.txt").write_text("dist")
        (output / "hello.txt").write_text("output")
        h = self._handler(dist, output)
        resolved = h.translate_path("/hello.txt")
        assert Path(resolved).read_text() == "output"

    def test_falls_back_to_dist(self, tmp_path):
        dist   = tmp_path / "dist";   dist.mkdir()
        output = tmp_path / "output"; output.mkdir()
        (dist / "shell.html").write_text("shell")
        h = self._handler(dist, output)
        resolved = h.translate_path("/shell.html")
        assert Path(resolved).read_text() == "shell"

    def test_missing_in_both_returns_dist_path(self, tmp_path):
        # Missing files still return a resolved path (from dist_dir);
        # SimpleHTTPRequestHandler will then 404 on it. What we're
        # verifying is the fallback doesn't crash / doesn't wander.
        dist   = tmp_path / "dist";   dist.mkdir()
        output = tmp_path / "output"; output.mkdir()
        h = self._handler(dist, output)
        resolved = h.translate_path("/nothing-here.txt")
        assert resolved.startswith(str(dist.resolve()))

    def test_path_escape_falls_through(self, tmp_path):
        # A `..` traversal attempt should NOT be resolved against
        # output_dir — it would let a user read arbitrary files on the box.
        dist   = tmp_path / "dist";   dist.mkdir()
        output = tmp_path / "output"; output.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("private")
        h = self._handler(dist, output)
        # SimpleHTTPRequestHandler already normalises the path before
        # translate_path sees it, so URL-level `..` is stripped upstream.
        # This test guards the belt: even if a malformed path reaches us,
        # the resolved candidate must NOT sit outside output_dir when we
        # claim to serve it from output_dir.
        resolved = h.translate_path("/../secret.txt")
        assert not resolved.endswith("secret.txt") or \
            Path(resolved).resolve().parent != tmp_path.resolve()


# ---------------------------------------------------------------------------
# Integration: boot server + fetch
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Grab an ephemeral port from the OS. Tests use a fresh port each
    run so parallel test workers don't collide.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server(tmp_path):
    """Boot `serve()` on a thread, tear it down cleanly on test exit.
    Yields (base_url, dist_dir, output_dir) so the test can populate the
    dirs and hit the server.
    """
    dist   = tmp_path / "dist";   dist.mkdir()
    output = tmp_path / "output"; output.mkdir()
    port = _free_port()

    thread_err = {}

    def _run():
        try:
            server.serve(
                output_dir=str(output),
                host="127.0.0.1",
                port=port,
                open_browser=False,
                dist_dir=str(dist),
            )
        except SystemExit:
            pass
        except Exception as e:  # pragma: no cover — captured for the test
            thread_err["exc"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Wait for the socket to accept — the server thread does its own
    # startup print, but a health-check on the port is more reliable.
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("server didn't come up in time")

    yield f"http://127.0.0.1:{port}", dist, output

    # KeyboardInterrupt breaks out of serve_forever; sending it via os.kill
    # isn't reliable across threads, so we make a nuisance connection then
    # shut the underlying socket. The daemon thread dies with the test.
    if thread_err:
        raise thread_err["exc"]


class TestServeIntegration:
    def test_serves_dist_html(self, running_server):
        base, dist, _ = running_server
        (dist / "index.html").write_text("<html><body>shell</body></html>")
        r = urllib.request.urlopen(base + "/index.html", timeout=1)
        assert r.status == 200
        assert b"shell" in r.read()

    def test_overlays_output_over_dist(self, running_server):
        # Same path exists in both; output wins.
        base, dist, output = running_server
        (dist   / "manifest.json").write_text('{"from": "dist"}')
        (output / "manifest.json").write_text('{"from": "output"}')
        r = urllib.request.urlopen(base + "/manifest.json", timeout=1)
        assert b'"from": "output"' in r.read()

    def test_output_only_file_served(self, running_server):
        base, _, output = running_server
        (output / "proof-config.json").write_text('{"familyName":"T"}')
        r = urllib.request.urlopen(base + "/proof-config.json", timeout=1)
        assert r.status == 200
        assert b"familyName" in r.read()

    def test_missing_returns_404(self, running_server):
        base, _, _ = running_server
        try:
            urllib.request.urlopen(base + "/does-not-exist", timeout=1)
        except urllib.error.HTTPError as e:
            assert e.code == 404
        else:  # pragma: no cover
            pytest.fail("expected 404")

    def test_ttf_content_type(self, running_server):
        # Some browsers refuse to load fonts served with generic
        # `application/octet-stream`. `font/ttf` is what the server should
        # advertise after we register the extra MIME types.
        base, _, output = running_server
        (output / "Any.ttf").write_bytes(b"\x00\x01\x00\x00fake-ttf")
        r = urllib.request.urlopen(base + "/Any.ttf", timeout=1)
        assert r.headers.get("Content-Type") == "font/ttf"

    def test_json_content_type(self, running_server):
        base, _, output = running_server
        (output / "x.json").write_text("{}")
        r = urllib.request.urlopen(base + "/x.json", timeout=1)
        assert r.headers.get("Content-Type") == "application/json"


# ---------------------------------------------------------------------------
# Package-level dist lookup
# ---------------------------------------------------------------------------

class TestWebappDistDir:
    def test_returns_real_path(self):
        # `_webapp_dist_dir` walks importlib.resources. As long as the
        # package's webapp/dist/ actually exists (built during package
        # install), we should get a valid path back.
        p = server._webapp_dist_dir()
        assert isinstance(p, Path)
        # The path may or may not exist depending on whether the wheel was
        # built — the function itself must not crash on lookup.
