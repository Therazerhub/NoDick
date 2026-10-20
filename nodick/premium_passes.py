"""User-bound return-token redemption, not proof of provider/ad completion.

Only SHA-256 token digests are persisted. Passes are separate from paid/lifetime
Premium, expire on UTC epoch seconds, and never extend an already active pass.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import re
import secrets
import time

_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}", re.ASCII)
PASS_SECONDS = 86400


def _digest(token: str) -> str | None:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def init_schema() -> None:
    """Additive, idempotent migration for fresh and existing SQLite/PG DBs."""
    from . import db
    db._execute("""CREATE TABLE IF NOT EXISTS premium_pass_tokens (
        token_hash TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL UNIQUE,
        expires_at BIGINT NOT NULL,
        ready INTEGER NOT NULL DEFAULT 0,
        shortened_url TEXT NOT NULL DEFAULT ''
    )""")
    db._execute("""CREATE TABLE IF NOT EXISTS premium_pass_entitlements (
        user_id BIGINT PRIMARY KEY,
        premium_until BIGINT NOT NULL
    )""")


@contextmanager
def _locked_user(user_id: int):
    """Serialize all pass writes, including when no token/entitlement exists.

    The pooled PG helper uses autocommit, so use a dedicated transactional
    connection like the quota code. Never hold a database lock over an API call.
    """
    from . import db
    db.ensure_user_exists(user_id)
    ph = "%s" if db._using_pg else "?"
    if db._using_pg:
        conn = db._new_pg_connection()
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM user_settings WHERE user_id = %s FOR UPDATE", (user_id,))
                if cur.fetchone() is None:
                    raise RuntimeError("Pass user disappeared before locking")
                yield cur, ph
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            try:
                cur.execute("SELECT user_id FROM user_settings WHERE user_id = ?", (user_id,))
                if cur.fetchone() is None:
                    raise RuntimeError("Pass user disappeared before locking")
                yield cur, ph
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()


def _until(cur, ph: str, user_id: int, now: int) -> int:
    cur.execute(f"SELECT premium_until FROM premium_pass_entitlements WHERE user_id = {ph}", (user_id,))
    row = cur.fetchone()
    until = int(row[0]) if row else 0
    return until if until > now else 0


def active_pass_until(user_id: int) -> int:
    from . import db
    ph = "%s" if db._using_pg else "?"
    row = db._fetchone(f"SELECT premium_until FROM premium_pass_entitlements WHERE user_id = {ph}", (user_id,))
    until = int(row["premium_until"]) if row else 0
    return until if until > int(time.time()) else 0


def has_active_pass(user_id: int) -> bool:
    return bool(active_pass_until(user_id))


def issue_pass(user_id: int, ttl_seconds: int = 1800) -> dict:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer")
    with _locked_user(user_id) as (cur, ph):
        now = int(time.time())
        until = _until(cur, ph, user_id, now)
        # At most one pending token and one entitlement row per user, ever.
        cur.execute(f"DELETE FROM premium_pass_tokens WHERE user_id = {ph}", (user_id,))
        if until:
            return {"status": "active", "premium_until": until}
        token = secrets.token_urlsafe(24)
        expires_at = now + ttl_seconds
        cur.execute(f"INSERT INTO premium_pass_tokens (token_hash, user_id, expires_at) VALUES ({ph}, {ph}, {ph})",
                    (_digest(token), user_id, expires_at))
        return {"status": "issued", "token": token, "expires_at": expires_at}


def mark_pass_ready(user_id: int, token: str, shortened_url: str) -> bool:
    digest = _digest(token)
    if digest is None or not isinstance(shortened_url, str) or not shortened_url:
        return False
    with _locked_user(user_id) as (cur, ph):
        cur.execute(f"UPDATE premium_pass_tokens SET ready = 1, shortened_url = {ph} "
                    f"WHERE user_id = {ph} AND token_hash = {ph} AND expires_at > {ph}",
                    (shortened_url, user_id, digest, int(time.time())))
        return cur.rowcount == 1


def cancel_pass(user_id: int, token: str) -> None:
    digest = _digest(token)
    if digest is None:
        return
    with _locked_user(user_id) as (cur, ph):
        cur.execute(f"DELETE FROM premium_pass_tokens WHERE user_id = {ph} AND token_hash = {ph} AND ready = 0",
                    (user_id, digest))


def pending_pass(user_id: int) -> dict | None:
    from . import db
    ph = "%s" if db._using_pg else "?"
    row = db._fetchone(f"SELECT shortened_url, expires_at FROM premium_pass_tokens "
                       f"WHERE user_id = {ph} AND ready = 1 AND expires_at > {ph}",
                       (user_id, int(time.time())))
    return {"shortened_url": row["shortened_url"], "expires_at": int(row["expires_at"])} if row else None


def redeem_pass(user_id: int, token: str) -> dict:
    digest = _digest(token)
    if digest is None:
        return {"status": "invalid"}
    with _locked_user(user_id) as (cur, ph):
        now = int(time.time())
        cur.execute(f"SELECT expires_at, ready FROM premium_pass_tokens WHERE user_id = {ph} AND token_hash = {ph}",
                    (user_id, digest))
        row = cur.fetchone()
        if row is None:
            return {"status": "invalid"}
        if int(row[0]) <= now:
            cur.execute(f"DELETE FROM premium_pass_tokens WHERE user_id = {ph}", (user_id,))
            return {"status": "expired"}
        until = _until(cur, ph, user_id, now)
        if until:
            cur.execute(f"DELETE FROM premium_pass_tokens WHERE user_id = {ph}", (user_id,))
            return {"status": "active", "premium_until": until}
        if not row[1]:
            return {"status": "invalid"}
        until = now + PASS_SECONDS
        cur.execute(f"INSERT INTO premium_pass_entitlements (user_id, premium_until) VALUES ({ph}, {ph}) "
                    "ON CONFLICT (user_id) DO UPDATE SET premium_until = excluded.premium_until", (user_id, until))
        cur.execute(f"DELETE FROM premium_pass_tokens WHERE user_id = {ph}", (user_id,))
        return {"status": "activated", "premium_until": until}


def revoke_pass(user_id: int) -> None:
    """Revoke entitlement and pending returns so revocation cannot be bypassed."""
    with _locked_user(user_id) as (cur, ph):
        cur.execute(f"DELETE FROM premium_pass_entitlements WHERE user_id = {ph}", (user_id,))
        cur.execute(f"DELETE FROM premium_pass_tokens WHERE user_id = {ph}", (user_id,))
