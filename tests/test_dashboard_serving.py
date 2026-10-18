"""The built dashboard is served at / with an SPA fallback, without shadowing server routes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from voiceagent.server import create_app


def _client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>dashboard</body></html>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    (dist / "favicon.svg").write_text("<svg/>")
    monkeypatch.setenv("VOICEAGENT_WEB_DIST", str(dist))
    return TestClient(create_app())


def test_root_and_deep_links_serve_index(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert "dashboard" in c.get("/").text
    r = c.get("/calls/abc123")
    assert r.status_code == 200 and "dashboard" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_assets_and_public_files(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/assets/app.js").text == "console.log('hi')"
    assert c.get("/favicon.svg").text == "<svg/>"


def test_server_routes_not_shadowed(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/api/does-not-exist").status_code == 404
    assert "dashboard" not in c.get("/api/does-not-exist").text
    ui = c.get("/ui")
    assert ui.status_code == 200 and "dashboard" not in ui.text


def test_path_traversal_is_not_served(tmp_path, monkeypatch):
    (tmp_path / "secret.txt").write_text("top secret")
    c = _client(tmp_path, monkeypatch)
    r = c.get("/..%2Fsecret.txt")
    assert "top secret" not in r.text


def test_no_dashboard_build_is_fine(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEAGENT_WEB_DIST", str(tmp_path / "missing"))
    from voiceagent import server

    monkeypatch.setattr(server, "_dashboard_dir", lambda: None)
    c = TestClient(create_app())
    assert c.get("/health").status_code == 200
    assert c.get("/").status_code == 404
