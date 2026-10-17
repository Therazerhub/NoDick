"""Telegram-authenticated community and sauce-finder data layer."""
from __future__ import annotations

import hashlib
import ipaddress
import math
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nodick import db

_ALLOWED_TAGS = {
    "classic", "cosplay", "animation", "studio", "performer", "amateur",
    "story", "vr", "vintage", "mystery", "discussion", "request",
}
_BLOCKED_HOSTS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "cutt.ly", "is.gd",
    "localhost",
}
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}
_SAFE_TEXT = re.compile(r"^[^<>\x00-\x08\x0b\x0c\x0e-\x1f]+$")
_ACTION_LOCK = threading.RLock()
_RATE_BUCKETS: dict[tuple[int, str, int], int] = {}


def _ph() -> str:
    return "%s" if db._using_pg else "?"


def _row_dict(row) -> dict:
    return dict(row) if row else {}


def normalize_url(raw: str) -> tuple[str, str, str]:
    raw = (raw or "").strip()
    if not raw or len(raw) > 2048:
        raise ValueError("Enter a valid link under 2,048 characters.")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("That link is malformed.") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Only direct HTTPS links are accepted.")
    if parsed.username or parsed.password:
        raise ValueError("Links containing credentials are not allowed.")
    if parsed.port not in (None, 443):
        raise ValueError("Non-standard ports are not allowed.")
    host = parsed.hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("That domain is invalid.") from exc
    if host in _BLOCKED_HOSTS or host.endswith((".local", ".localhost")):
        raise ValueError("URL shorteners and local addresses are not accepted.")
    # Browsers accept several non-canonical numeric forms for loopback (e.g.
    # 2130706433, 0x7f000001, 127.1). Reject all numeric-looking hostnames.
    if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", host, re.I):
        raise ValueError("Numeric hosts are not accepted.")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private or reserved network addresses are not accepted.")
    if address is not None:
        raise ValueError("Use a named website, not a raw IP address.")
    query = sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in _TRACKING)
    path = parsed.path or "/"
    normalized = urlunsplit(("https", host, path, urlencode(query, doseq=True), ""))
    return normalized, host, hashlib.sha256(normalized.encode()).hexdigest()


def consume_rate(user_id: int, action: str, limit: int, window: int) -> None:
    """Small process-local abuse brake; DB constraints remain authoritative."""
    bucket = int(time.time()) // window
    key = (user_id, action, bucket)
    with _ACTION_LOCK:
        count = _RATE_BUCKETS.get(key, 0)
        if count >= limit:
            raise RuntimeError(f"Slow down. Try again in {window - int(time.time()) % window}s.")
        _RATE_BUCKETS[key] = count + 1
        if len(_RATE_BUCKETS) > 5000:
            for old in [k for k in _RATE_BUCKETS if k[2] < bucket - 2]:
                _RATE_BUCKETS.pop(old, None)


def safe_text(value: str, *, minimum: int, maximum: int, field: str) -> str:
    value = " ".join((value or "").strip().split())
    if len(value) < minimum or len(value) > maximum or not _SAFE_TEXT.match(value):
        raise ValueError(f"{field} must be {minimum}–{maximum} safe characters.")
    return value


def public_alias(user_id: int) -> str:
    adjectives = ("Night", "Velvet", "Neon", "Quiet", "Silver", "Nova", "Midnight", "Violet")
    nouns = ("Fox", "Scout", "Owl", "Comet", "Raven", "Lynx", "Muse", "Signal")
    digest = hashlib.blake2s(str(user_id).encode(), digest_size=4).digest()
    return f"{adjectives[digest[0] % len(adjectives)]}{nouns[digest[1] % len(nouns)]}{int.from_bytes(digest[2:], 'big') % 100:02d}"


def init_community_db() -> None:
    serial = "BIGSERIAL" if db._using_pg else "INTEGER"
    pk = "PRIMARY KEY" if db._using_pg else "PRIMARY KEY AUTOINCREMENT"
    integer = "BIGINT" if db._using_pg else "INTEGER"
    statements = [
        f"""CREATE TABLE IF NOT EXISTS community_profiles (
            user_id {integer} PRIMARY KEY, alias TEXT NOT NULL, reputation INTEGER NOT NULL DEFAULT 0,
            accepted_policy INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS community_posts (
            id {serial} {pk}, author_id {integer} NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL, source_domain TEXT NOT NULL,
            source_hash TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
            score INTEGER NOT NULL DEFAULT 0, vote_count INTEGER NOT NULL DEFAULT 0,
            save_count INTEGER NOT NULL DEFAULT 0, comment_count INTEGER NOT NULL DEFAULT 0,
            report_count INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_activity_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS community_votes (
            post_id {integer} NOT NULL, user_id {integer} NOT NULL, value INTEGER NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(post_id, user_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS community_saves (
            post_id {integer} NOT NULL, user_id {integer} NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(post_id, user_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS community_comments (
            id {serial} {pk}, post_id {integer} NOT NULL, author_id {integer} NOT NULL,
            body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        f"""CREATE TABLE IF NOT EXISTS community_reports (
            id {serial} {pk}, reporter_id {integer} NOT NULL, post_id {integer} NOT NULL,
            reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(reporter_id, post_id)
        )""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_community_posts_feed ON community_posts(status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_community_posts_domain ON community_posts(source_domain, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_community_posts_hash ON community_posts(source_hash)",
        "CREATE INDEX IF NOT EXISTS idx_community_comments_post ON community_comments(post_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_community_reports_status ON community_reports(status, created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_community_active_source ON community_posts(source_hash) WHERE status='active'",
    ]
    for sql in statements + indexes:
        db._execute(sql)


def ensure_profile(user_id: int, *, accept_policy: bool = False) -> dict:
    db.ensure_user_exists(user_id)
    ph = _ph()
    row = db._fetchone(f"SELECT * FROM community_profiles WHERE user_id={ph}", (user_id,))
    if not row:
        alias = public_alias(user_id)
        db._execute(
            f"INSERT INTO community_profiles(user_id,alias,accepted_policy) VALUES({ph},{ph},{ph})",
            (user_id, alias, 1 if accept_policy else 0),
        )
    elif accept_policy and not row["accepted_policy"]:
        db._execute(f"UPDATE community_profiles SET accepted_policy=1 WHERE user_id={ph}", (user_id,))
    return _row_dict(db._fetchone(f"SELECT * FROM community_profiles WHERE user_id={ph}", (user_id,)))


def update_profile(user_id: int, alias: str | None = None, *, accept_policy: bool = False) -> dict:
    ensure_profile(user_id)
    ph = _ph()
    if alias is not None:
        alias = safe_text(alias, minimum=3, maximum=24, field="Alias")
        if not re.fullmatch(r"[A-Za-z0-9_]+", alias):
            raise ValueError("Alias may contain letters, numbers and underscores only.")
        existing = db._fetchone(f"SELECT user_id FROM community_profiles WHERE LOWER(alias)=LOWER({ph}) AND user_id<>{ph}", (alias, user_id))
        if existing:
            raise ValueError("That alias is already taken.")
        db._execute(f"UPDATE community_profiles SET alias={ph} WHERE user_id={ph}", (alias, user_id))
    if accept_policy:
        db._execute(f"UPDATE community_profiles SET accepted_policy=1 WHERE user_id={ph}", (user_id,))
    return ensure_profile(user_id)


def _post_projection(row, viewer_id: int) -> dict:
    data = _row_dict(row)
    post_id = int(data["id"])
    vote = db._fetchone(f"SELECT value FROM community_votes WHERE post_id={_ph()} AND user_id={_ph()}", (post_id, viewer_id))
    saved = db._fetchone(f"SELECT 1 AS yes FROM community_saves WHERE post_id={_ph()} AND user_id={_ph()}", (post_id, viewer_id))
    profile = ensure_profile(int(data["author_id"]))
    age_hours = max(0, (datetime.now(timezone.utc) - _as_datetime(data["created_at"])).total_seconds() / 3600)
    heat = (int(data.get("score") or 0) * 3 + int(data.get("save_count") or 0) * 2 + int(data.get("comment_count") or 0)) / math.pow(age_hours + 2, 0.85)
    return {
        "id": post_id, "title": data["title"], "description": data["description"],
        "domain": data["source_domain"], "tags": [t for t in (data.get("tags") or "").split(",") if t],
        "score": int(data.get("score") or 0), "votes": int(data.get("vote_count") or 0),
        "saves": int(data.get("save_count") or 0), "comments": int(data.get("comment_count") or 0),
        "viewer_vote": int(vote["value"]) if vote else 0, "saved": bool(saved),
        "author": {"alias": profile["alias"], "tier": reputation_tier(int(profile.get("reputation") or 0))},
        "is_mine": int(data["author_id"]) == viewer_id, "created_at": str(data["created_at"]),
        "heat": round(heat, 3),
    }


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=dt.tzinfo or timezone.utc)


def reputation_tier(points: int) -> str:
    if points >= 120: return "Curator"
    if points >= 50: return "Scout"
    if points >= 15: return "Contributor"
    return "New"


def create_post(user_id: int, title: str, description: str, source_url: str, tags: list[str]) -> dict:
    consume_rate(user_id, "post", 3, 3600)
    profile = ensure_profile(user_id)
    if not profile.get("accepted_policy"):
        raise PermissionError("Accept the community rules before posting.")
    title = safe_text(title, minimum=4, maximum=90, field="Title")
    description = safe_text(description, minimum=8, maximum=280, field="Description")
    url, domain, digest = normalize_url(source_url)
    cleaned_tags = list(dict.fromkeys(t.lower().strip() for t in (tags or []) if t.lower().strip() in _ALLOWED_TAGS))[:3]
    ph = _ph()
    duplicate = db._fetchone(
        f"SELECT id FROM community_posts WHERE source_hash={ph} AND status='active' LIMIT 1", (digest,)
    )
    if duplicate:
        raise FileExistsError(str(duplicate["id"]))
    if db._using_pg:
        result = db._execute(
            "INSERT INTO community_posts(author_id,title,description,source_url,source_domain,source_hash,tags) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (user_id, title, description, url, domain, digest, ",".join(cleaned_tags)),
        )
        post_id = db._lastrowid(result)
    else:
        result = db._execute(
            "INSERT INTO community_posts(author_id,title,description,source_url,source_domain,source_hash,tags) VALUES(?,?,?,?,?,?,?)",
            (user_id, title, description, url, domain, digest, ",".join(cleaned_tags)),
        )
        post_id = db._lastrowid(result)
    return get_post(post_id, user_id)


def get_post(post_id: int, viewer_id: int) -> dict | None:
    row = db._fetchone(f"SELECT * FROM community_posts WHERE id={_ph()} AND status='active'", (post_id,))
    return _post_projection(row, viewer_id) if row else None


def list_posts(viewer_id: int, *, sort: str = "rising", query: str = "", page: int = 0, per_page: int = 20) -> tuple[list[dict], int]:
    page, per_page = max(0, min(page, 10000)), max(1, min(per_page, 40))
    ph = _ph(); params: list = []
    where = "status='active'"
    if query.strip():
        term = f"%{query.strip()[:80]}%"; op = "ILIKE" if db._using_pg else "LIKE"
        where += f" AND (title {op} {ph} OR description {op} {ph} OR source_domain {op} {ph} OR tags {op} {ph})"
        params += [term] * 4
    order = {"new": "id DESC", "top": "score DESC, save_count DESC, id DESC", "discussed": "comment_count DESC, id DESC"}.get(sort, "last_activity_at DESC, id DESC")
    total_row = db._fetchone(f"SELECT COUNT(*) AS cnt FROM community_posts WHERE {where}", tuple(params))
    rows = db._fetchall(f"SELECT * FROM community_posts WHERE {where} ORDER BY {order} LIMIT {ph} OFFSET {ph}", tuple(params + [per_page, page * per_page]))
    items = [_post_projection(row, viewer_id) for row in rows]
    if sort == "rising": items.sort(key=lambda item: (item["heat"], item["id"]), reverse=True)
    return items, int(total_row["cnt"] if total_row else 0)


def set_vote(user_id: int, post_id: int, value: int) -> dict:
    if value not in (-1, 0, 1): raise ValueError("Vote must be -1, 0 or 1.")
    if not get_post(post_id, user_id): raise LookupError("Post not found.")
    ph = _ph()
    with _ACTION_LOCK:
        old = db._fetchone(f"SELECT value FROM community_votes WHERE post_id={ph} AND user_id={ph}", (post_id, user_id))
        old_value = int(old["value"]) if old else 0
        if value == old_value:
            return get_post(post_id, user_id)
        if value == 0:
            db._execute(f"DELETE FROM community_votes WHERE post_id={ph} AND user_id={ph}", (post_id, user_id))
        elif old:
            db._execute(f"UPDATE community_votes SET value={ph},updated_at=CURRENT_TIMESTAMP WHERE post_id={ph} AND user_id={ph}", (value, post_id, user_id))
        else:
            db._execute(f"INSERT INTO community_votes(post_id,user_id,value) VALUES({ph},{ph},{ph})", (post_id, user_id, value))
        delta_count = (1 if value else 0) - (1 if old_value else 0)
        db._execute(f"UPDATE community_posts SET score=score+{ph},vote_count=vote_count+{ph},last_activity_at=CURRENT_TIMESTAMP WHERE id={ph}", (value-old_value, delta_count, post_id))
        author = db._fetchone(f"SELECT author_id FROM community_posts WHERE id={ph}", (post_id,))
        rep_delta = (1 if value == 1 else 0) - (1 if old_value == 1 else 0)
        if author and int(author["author_id"]) != user_id and rep_delta:
            floor_fn = "GREATEST" if db._using_pg else "MAX"
            db._execute(f"UPDATE community_profiles SET reputation={floor_fn}(0,reputation+{ph}) WHERE user_id={ph}", (rep_delta, int(author["author_id"])))
    return get_post(post_id, user_id)


def set_saved(user_id: int, post_id: int, saved: bool) -> dict:
    if not get_post(post_id, user_id): raise LookupError("Post not found.")
    ph = _ph()
    with _ACTION_LOCK:
        existing = db._fetchone(f"SELECT 1 AS yes FROM community_saves WHERE post_id={ph} AND user_id={ph}", (post_id, user_id))
        if saved and not existing:
            db._execute(f"INSERT INTO community_saves(post_id,user_id) VALUES({ph},{ph})", (post_id, user_id))
            db._execute(f"UPDATE community_posts SET save_count=save_count+1 WHERE id={ph}", (post_id,))
        elif not saved and existing:
            db._execute(f"DELETE FROM community_saves WHERE post_id={ph} AND user_id={ph}", (post_id, user_id))
            db._execute(f"UPDATE community_posts SET save_count=CASE WHEN save_count>0 THEN save_count-1 ELSE 0 END WHERE id={ph}", (post_id,))
    return get_post(post_id, user_id)


def list_comments(post_id: int, viewer_id: int) -> list[dict]:
    if not get_post(post_id, viewer_id):
        raise LookupError("Post not found.")
    ph = _ph(); rows = db._fetchall(f"SELECT * FROM community_comments WHERE post_id={ph} AND status='active' ORDER BY id ASC LIMIT 100", (post_id,))
    result = []
    for row in rows:
        data = _row_dict(row); profile = ensure_profile(int(data["author_id"]))
        result.append({"id": int(data["id"]), "body": data["body"], "author": {"alias": profile["alias"], "tier": reputation_tier(int(profile.get("reputation") or 0))}, "is_mine": int(data["author_id"]) == viewer_id, "created_at": str(data["created_at"])})
    return result


def create_comment(user_id: int, post_id: int, body: str) -> dict:
    consume_rate(user_id, "comment", 12, 600)
    if not get_post(post_id, user_id): raise LookupError("Post not found.")
    body = safe_text(body, minimum=2, maximum=500, field="Comment")
    if "http://" in body.lower() or "https://" in body.lower(): raise ValueError("Put links in posts, not comments.")
    ph = _ph()
    if db._using_pg:
        result = db._execute("INSERT INTO community_comments(post_id,author_id,body) VALUES(%s,%s,%s) RETURNING id", (post_id, user_id, body))
        comment_id = db._lastrowid(result)
    else:
        result = db._execute("INSERT INTO community_comments(post_id,author_id,body) VALUES(?,?,?)", (post_id, user_id, body))
        comment_id = db._lastrowid(result)
    db._execute(f"UPDATE community_posts SET comment_count=comment_count+1,last_activity_at=CURRENT_TIMESTAMP WHERE id={ph}", (post_id,))
    return next(item for item in list_comments(post_id, user_id) if item["id"] == comment_id)


def report_post(user_id: int, post_id: int, reason: str, details: str = "") -> bool:
    consume_rate(user_id, "report", 8, 3600)
    reasons = {"spam", "broken", "dangerous", "misleading", "other"}
    if reason not in reasons: raise ValueError("Choose a valid report reason.")
    post = db._fetchone(f"SELECT author_id FROM community_posts WHERE id={_ph()} AND status='active'", (post_id,))
    if not post: raise LookupError("Post not found.")
    if int(post["author_id"]) == user_id: raise ValueError("You cannot report your own post.")
    details = " ".join((details or "").strip().split())[:240]
    ph = _ph(); existing = db._fetchone(f"SELECT id FROM community_reports WHERE reporter_id={ph} AND post_id={ph}", (user_id, post_id))
    if existing: return False
    db._execute(f"INSERT INTO community_reports(reporter_id,post_id,reason,details) VALUES({ph},{ph},{ph},{ph})", (user_id, post_id, reason, details))
    db._execute(f"UPDATE community_posts SET report_count=report_count+1 WHERE id={ph}", (post_id,))
    return True


def open_link(user_id: int, post_id: int) -> str:
    ph = _ph()
    post = db._fetchone(f"SELECT source_url,status FROM community_posts WHERE id={ph}", (post_id,))
    open_reports = db._fetchone(f"SELECT COUNT(*) AS cnt FROM community_reports WHERE post_id={ph} AND status='open'", (post_id,))
    if not post or post["status"] != "active" or int(open_reports["cnt"] if open_reports else 0) >= 3:
        raise PermissionError("This link is under review and cannot be opened.")
    normalize_url(str(post["source_url"]))
    return str(post["source_url"])


def community_stats(user_id: int) -> dict:
    profile = ensure_profile(user_id); ph = _ph()
    posts = db._fetchone(f"SELECT COUNT(*) AS cnt FROM community_posts WHERE author_id={ph} AND status='active'", (user_id,))
    saves = db._fetchone(f"SELECT COUNT(*) AS cnt FROM community_saves WHERE user_id={ph}", (user_id,))
    return {"profile": {"alias": profile["alias"], "reputation": int(profile.get("reputation") or 0), "tier": reputation_tier(int(profile.get("reputation") or 0)), "accepted_policy": bool(profile.get("accepted_policy"))}, "posts": int(posts["cnt"] if posts else 0), "saved": int(saves["cnt"] if saves else 0), "tags": sorted(_ALLOWED_TAGS)}


def moderation_queue(limit: int = 50) -> list[dict]:
    """Safe report queue for the owner; never includes reporter identities."""
    ph = _ph()
    rows = db._fetchall(
        f"SELECT r.id AS report_id,r.reason,r.details,r.created_at,p.id AS post_id,"
        f"p.title,p.source_domain,p.report_count FROM community_reports r "
        f"JOIN community_posts p ON p.id=r.post_id WHERE r.status='open' "
        f"ORDER BY r.id ASC LIMIT {ph}", (max(1, min(limit, 100)),)
    )
    return [dict(row) for row in rows]


def moderate_report(moderator_id: int, report_id: int, action: str) -> dict:
    if action not in {"dismiss", "hide", "remove"}:
        raise ValueError("Unknown moderation action.")
    ph = _ph()
    report = db._fetchone(f"SELECT * FROM community_reports WHERE id={ph} AND status='open'", (report_id,))
    if not report:
        raise LookupError("Report not found.")
    if action in {"hide", "remove"}:
        db._execute(f"UPDATE community_posts SET status={ph} WHERE id={ph}", ("hidden" if action == "hide" else "removed", int(report["post_id"])))
    db._execute(f"UPDATE community_reports SET status={ph} WHERE id={ph}", ("dismissed" if action == "dismiss" else "actioned", report_id))
    return {"report_id": report_id, "action": action, "moderator": public_alias(moderator_id)}
