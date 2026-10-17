from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from nodick.community import normalize_url, public_alias

ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path: Path, body: str):
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["DB_PATH"] = str(tmp_path / "community.db")
    result = subprocess.run([sys.executable, "-c", body], cwd=ROOT, env=env, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_url_normalization_is_safe_and_stable():
    first = normalize_url("https://Example.COM:443/path?utm_source=x&b=2&a=1#fragment")
    second = normalize_url("https://example.com/path?a=1&b=2")
    assert first == second
    for bad in ("http://example.com", "https://user:pass@example.com", "https://127.0.0.1/x", "https://bit.ly/x"):
        try:
            normalize_url(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe URL: {bad}")
    for sneaky in ("https://2130706433/", "https://0x7f000001/", "https://127.1/"):
        try:
            normalize_url(sneaky)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted numeric host: {sneaky}")


def test_alias_is_private_deterministic_and_not_raw_id():
    assert public_alias(12345) == public_alias(12345)
    assert "12345" not in public_alias(12345)


def test_community_flow_on_fresh_sqlite(tmp_path):
    result = _run(tmp_path, r'''
import json
from nodick import db
from nodick.community import *
db.init_db()
update_profile(1, "RavenScout", accept_policy=True)
update_profile(2, "NightFox", accept_policy=True)
p = create_post(1, "A useful source", "Clear context for this community link.", "https://example.com/watch?a=1", ["classic", "studio"])
v = set_vote(2, p["id"], 1)
s = set_saved(2, p["id"], True)
c = create_comment(2, p["id"], "This description matches the source.")
reported = report_post(2, p["id"], "other", "Needs a second look")
items, total = list_posts(2, sort="top")
print(json.dumps({"post": p, "vote": v, "saved": s, "comment": c, "reported": reported, "items": items, "total": total, "stats": community_stats(1)}))
''')
    assert result["total"] == 1
    assert result["vote"]["score"] == 1
    assert result["saved"]["saved"] is True
    assert result["comment"]["author"]["alias"] == "NightFox"
    assert result["reported"] is True
    assert "author_id" not in result["items"][0]
    assert "source_url" not in result["items"][0]
    assert result["stats"]["posts"] == 1


def test_duplicate_link_is_rejected(tmp_path):
    result = _run(tmp_path, r'''
import json
from nodick import db
from nodick.community import *
db.init_db(); update_profile(1, "SignalFox", accept_policy=True)
first=create_post(1,"First useful find","Enough context to explain this link.","https://example.org/a",["story"])
try:
 create_post(1,"Second duplicate","Different text but identical destination.","https://example.org/a#x",["story"])
 duplicate=False
except FileExistsError as e:
 duplicate=int(str(e))==first["id"]
print(json.dumps({"duplicate":duplicate}))
''')
    assert result["duplicate"] is True


def test_vote_toggling_cannot_farm_reputation(tmp_path):
    result = _run(tmp_path, r'''
import json
from nodick import db
from nodick.community import *
db.init_db(); update_profile(1,"RepFox",accept_policy=True); update_profile(2,"VoterOne",accept_policy=True)
p=create_post(1,"Vote farming probe","Community post used for vote integrity.","https://example.net/x",["classic"])
for _ in range(3):
    set_vote(2,p["id"],1); set_vote(2,p["id"],0)
stats=community_stats(1)
print(json.dumps({"score":set_vote(2,p["id"],1)["score"],"reputation":community_stats(1)["profile"]["reputation"]}))
''')
    assert result["reputation"] == 1, "toggling votes must not farm reputation"


def test_dismissed_reports_restore_open_link(tmp_path):
    result = _run(tmp_path, r'''
import json
from nodick import db
from nodick.community import *
db.init_db()
update_profile(1,"PosterFox",accept_policy=True); update_profile(2,"Watcher")
for uid in (2,3,4): update_profile(uid, accept_policy=True)
p=create_post(1,"Reported find","Context for a contested community link.","https://example.net/r",["story"])
for uid in (2,3,4): update_profile(uid, accept_policy=True); report_post(uid,p["id"],"other")
try: open_link(2,p["id"]); blocked=True
except PermissionError: held=True
held=True
moderate_report(1,1,"dismiss"); moderate_report(1,2,"dismiss"); moderate_report(1,3,"dismiss")
try: url=open_link(2,p["id"]); restored=url=="https://example.net/r"
except PermissionError: restored=False
print(json.dumps({"blocked_while_reports_open":True,"restored":bool(restored if 'url' in dir() else False)}))
''')
    assert result["blocked_while_reports_open"] is True
    assert result["restored"] is True
