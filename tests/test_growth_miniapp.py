from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from nodick import web
from nodick import db
from nodick.telegram import app, keyboards
from nodick.utils import title_from_filename_or_caption


def _signed_init_data(token: str, user_id: int, *, auth_date: int | None = None) -> str:
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "query-test",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_init_data_hmac_and_expiry():
    token = "123:test-token"
    signed = _signed_init_data(token, 42, auth_date=1_700_000_000)
    assert web.validate_init_data(signed, token, now=1_700_000_100)["user_id"] == 42
    with pytest.raises(web.AuthError):
        web.validate_init_data(signed.replace("Test", "Fake"), token, now=1_700_000_100)
    with pytest.raises(web.AuthError, match="expired"):
        web.validate_init_data(signed, token, now=1_700_004_000)


def test_growth_schema_declared_for_both_backends():
    source = inspect.getsource(db)
    assert source.count("CREATE TABLE IF NOT EXISTS growth_events") >= 2
    for column in ("user_id", "event_type", "source", "payload", "video_id", "created_at"):
        assert source.count(column) >= 2


def _request(server, path: str, token: str | None = None, user_id: int = 1, method: str = "GET"):
    headers = {}
    if token:
        headers["Authorization"] = "tma " + _signed_init_data(token, user_id)
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}{path}", headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_mini_app_routes_and_authorization_boundary(monkeypatch):
    token = "123:test-token"
    seen = []
    saved = []
    monkeypatch.setattr(web, "record_growth_event", lambda *args, **kwargs: True)
    monkeypatch.setattr(web, "get_user_dashboard", lambda uid: seen.append(uid) or {"user": uid})
    monkeypatch.setattr(web, "get_categories", lambda: [{"category": "Studio", "count": 1}])
    monkeypatch.setattr(web, "get_catalog", lambda **kwargs: ([{"id": 8, "title": "Title", "category": "Studio", "duration": 60, "saved": 0}], 1))
    monkeypatch.setattr(web, "get_favorites", lambda *args, **kwargs: ([], 0))
    monkeypatch.setattr(web, "get_video", lambda video_id: {"id": video_id})
    monkeypatch.setattr(web, "set_favorite", lambda user_id, video_id, state: saved.append((user_id, video_id, state)))
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(bot_token=token, bot_username="TestBot"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(server, "/")
        assert (status, body) == (200, b"NoDick is alive")
        status, headers, body = _request(server, "/app/")
        assert status == 200 and b"PRIVATE LIBRARY" in body
        assert "Content-Security-Policy" in headers
        assert "X-Frame-Options" not in headers
        assert "https://web.telegram.org" in headers["Content-Security-Policy"]
        assert _request(server, "/api/dashboard")[0] == 401
        status, _, body = _request(server, "/api/dashboard", token, user_id=77)
        assert status == 200 and json.loads(body) == {"user": 77}
        assert seen == [77]
        status, _, body = _request(server, "/api/catalog", token, user_id=77)
        item = json.loads(body)["items"][0]
        assert status == 200 and item["open_url"].endswith("TestBot?start=vid_8")
        assert "file_id" not in item and "source_chat_id" not in item
        assert _request(server, "/api/saved/8", token, user_id=77, method="POST")[0] == 200
        assert _request(server, "/api/saved/8", token, user_id=88, method="DELETE")[0] == 200
        assert saved == [(77, 8, True), (88, 8, False)]
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_survives_growth_analytics_failure(monkeypatch):
    token = "123:test-token"
    monkeypatch.setattr(web, "record_growth_event", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("analytics down")))
    monkeypatch.setattr(web, "get_user_dashboard", lambda uid: {"user": uid})
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(bot_token=token, bot_username="TestBot"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(server, "/api/dashboard", token, user_id=77)
        assert status == 200 and json.loads(body) == {"user": 77}
    finally:
        server.shutdown()
        server.server_close()


def test_api_failure_returns_json_instead_of_hanging(monkeypatch):
    token = "123:test-token"
    monkeypatch.setattr(web, "get_categories", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(bot_token=token, bot_username="TestBot"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(server, "/api/categories", token, user_id=77)
        assert status == 500
        assert "temporarily unavailable" in json.loads(body)["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_web_client_has_timeout_and_non_blocking_secondary_boot():
    source = (web.ASSET_ROOT / "app.js").read_text()
    assert "AbortController" in source
    assert "TIMEOUT=12000" in source
    assert "Promise.allSettled" in source
    assert "replaceChildren" in source


def test_postgres_helpers_use_bounded_pool_and_query_deadlines():
    source = inspect.getsource(db)
    assert "ThreadedConnectionPool" in source
    assert "connect_timeout" in source
    assert "statement_timeout=8000" in source
    assert "lock_timeout=3000" in source
    assert "_pg_conn = None" not in source


def test_mini_app_html_and_javascript_are_not_cached(monkeypatch):
    token = "123:test-token"
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(bot_token=token, bot_username="TestBot"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _request(server, "/app/")[1]["Cache-Control"] == "no-store"
        assert _request(server, "/app/assets/app.js")[1]["Cache-Control"] == "no-store"
    finally:
        server.shutdown()
        server.server_close()


def test_force_join_membership_and_malformed_status(monkeypatch):
    assert app._member_has_access(SimpleNamespace(status="restricted", is_member=True))
    assert not app._member_has_access(SimpleNamespace(status="restricted", is_member=False))
    assert app._member_has_access(SimpleNamespace(status="administrator"))
    assert not app._member_has_access(SimpleNamespace(status="left"))
    monkeypatch.setattr(app, "get_bot_setting", lambda *_args: "{broken")
    status = app._force_join_config()
    assert status["enabled"] is True and status["valid"] is False


def test_webapp_button_only_when_configured(monkeypatch):
    monkeypatch.setattr(keyboards, "get_bot_setting", lambda *_args: "")
    monkeypatch.setattr(keyboards, "is_user_premium", lambda _uid: False)
    monkeypatch.setattr(keyboards, "settings", SimpleNamespace(mini_app_url="", admin_id=999))
    without = keyboards.main_menu(1)
    assert all(button.web_app is None for row in without.inline_keyboard for button in row)
    monkeypatch.setattr(keyboards, "settings", SimpleNamespace(mini_app_url="https://example.test/app/", admin_id=999))
    with_app = keyboards.main_menu(1)
    assert with_app.inline_keyboard[0][0].web_app.url == "https://example.test/app/"


def test_placeholder_caption_uses_metadata_and_omits_unknown_duration():
    caption = app._local_video_caption(
        {"title": "video 2", "duration": 0, "view_count": 3, "category": "Studio", "source_channel": "Source"},
        {"stashdb_performer": ",Performer,"},
    )
    assert "Performer — Studio" in caption
    assert "video 2" not in caption.lower()
    assert "⏱" not in caption
    assert "👁 4" in caption


def test_mini_app_normalizes_placeholder_titles():
    item = web._safe_video(
        {"id": 9, "title": "video 2", "category": "Studio", "duration": 0},
        "TestBot",
    )
    assert item["title"] == "Studio selection"
    assert "video 2" not in item["title"].lower()
    assert item["open_url"].endswith("TestBot?start=vid_9")


def test_valid_local_caption_keeps_title_and_known_duration():
    caption = app._local_video_caption(
        {"title": "Useful_Title.mp4", "duration": 65, "view_count": 0, "category": None}, None
    )
    assert "Useful Title" in caption
    assert "⏱ 1:05 | 👁 1" in caption


def test_import_title_rejects_placeholders_and_prefers_useful_caption():
    assert title_from_filename_or_caption("video 2.mp4", "A Useful Scene", "Video_99") == "A Useful Scene"
    assert title_from_filename_or_caption("file.mp4", None, "Video_99") == ""
    assert title_from_filename_or_caption("12345.mkv", None, "File_12") == ""
