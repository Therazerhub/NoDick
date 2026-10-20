"""Isolated disposable DBs only; environment fixed before frozen settings import."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = """
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock
import hashlib
import re
import threading
from nodick import db, premium_passes as passes
db.init_db()
UID = 987654321
NOW = 1800000000
URL = 'https://short.example/return'
def ready(uid=UID):
    result = passes.issue_pass(uid)
    assert result['status'] == 'issued'
    assert passes.mark_pass_ready(uid, result['token'], URL)
    return result['token']
"""


def run(tmp_path, body, prefix=PREFIX):
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["DB_PATH"] = str(tmp_path / "passes.sqlite")
    env["ADMIN_ID"] = "1"
    result = subprocess.run([sys.executable, "-c", prefix + '\n' + textwrap.dedent(body)],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_issue_hash_only_and_ready_retry(tmp_path):
    run(tmp_path, """
    with patch.object(passes.time, 'time', return_value=NOW):
        issued = passes.issue_pass(UID)
        token = issued['token']
        assert re.fullmatch(r'[A-Za-z0-9_-]{32}', token)
        assert issued == {'status': 'issued', 'token': token, 'expires_at': NOW + 1800}
        row = dict(db._fetchone('SELECT * FROM premium_pass_tokens'))
        assert row == dict(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=UID,
                           expires_at=NOW+1800, ready=0, shortened_url='')
        assert token not in str(row)
        assert passes.pending_pass(UID) is None
        assert passes.redeem_pass(UID, token) == {'status': 'invalid'}
        assert not passes.mark_pass_ready(UID, token, '')
        assert passes.mark_pass_ready(UID, token, URL)
        assert passes.mark_pass_ready(UID, token, URL)
        assert passes.pending_pass(UID) == {'shortened_url': URL, 'expires_at': NOW+1800}
        passes.cancel_pass(UID, token)  # successful URLs survive cleanup/retries
        assert passes.pending_pass(UID)
    """)


def test_full_day_replay_and_no_stack(tmp_path):
    run(tmp_path, """
    with patch.object(passes.time, 'time', return_value=NOW):
        token = ready()
        assert passes.redeem_pass(UID, token) == {'status': 'activated', 'premium_until': NOW+86400}
        assert passes.redeem_pass(UID, token) == {'status': 'invalid'}
        assert passes.pending_pass(UID) is None
        assert passes.active_pass_until(UID) == NOW+86400
        assert passes.has_active_pass(UID)
        assert db.is_user_premium(UID)
        assert db.get_user_quota(UID)['is_premium']
        assert db.increment_quota(UID)
    with patch.object(passes.time, 'time', return_value=NOW+3600):
        assert passes.issue_pass(UID) == {'status': 'active', 'premium_until': NOW+86400}
        assert not db._fetchall('SELECT * FROM premium_pass_tokens')
    with patch.object(passes.time, 'time', return_value=NOW+86400):
        assert passes.active_pass_until(UID) == 0
        assert not passes.has_active_pass(UID)
        assert not db.is_user_premium(UID)
        new_token = ready()
        assert passes.redeem_pass(UID, new_token)['premium_until'] == NOW+172800
    """)


@pytest.mark.parametrize('bad', [None, '', 'x'*31, 'x'*33, 'é'*32, 'x'*31+'!', 'x'*32+'\n', ' '+'x'*32, 123])
def test_exact_validation(tmp_path, bad):
    run(tmp_path, f"""
    token = ready()
    bad = {bad!r}
    assert passes.redeem_pass(UID, bad) == {{'status': 'invalid'}}
    assert not passes.mark_pass_ready(UID, bad, URL)
    passes.cancel_pass(UID, bad)
    assert passes.pending_pass(UID)
    assert not passes.has_active_pass(UID)
    """)


def test_wrong_user_expiry_boundary_cancel_and_replacement(tmp_path):
    run(tmp_path, """
    with patch.object(passes.time, 'time', return_value=NOW):
        old = passes.issue_pass(UID)['token']
        token = ready()
        assert passes.redeem_pass(UID, old)['status'] == 'invalid'
        assert not passes.mark_pass_ready(UID, old, URL)
        passes.cancel_pass(UID, old)
        assert passes.redeem_pass(UID+1, token)['status'] == 'invalid'
        assert not passes.mark_pass_ready(UID+1, token, URL)
        passes.cancel_pass(UID+1, token)
        assert passes.pending_pass(UID)
    with patch.object(passes.time, 'time', return_value=NOW+1800):
        assert passes.pending_pass(UID) is None
        assert not passes.mark_pass_ready(UID, token, URL)
        assert passes.redeem_pass(UID, token)['status'] == 'expired'
        assert passes.redeem_pass(UID, token)['status'] == 'invalid'
        assert not passes.has_active_pass(UID)
        pending = passes.issue_pass(UID)['token']
        passes.cancel_pass(UID, pending)
        assert passes.redeem_pass(UID, pending)['status'] == 'invalid'
        for _ in range(30):
            passes.issue_pass(UID, ttl_seconds=1)
        assert len(db._fetchall('SELECT * FROM premium_pass_tokens')) == 1
    """)


@pytest.mark.parametrize('ttl', [0, -1, True, 1.5, '1800', None])
def test_invalid_ttl(tmp_path, ttl):
    run(tmp_path, f"""
    try:
        passes.issue_pass(UID, {ttl!r})
    except ValueError:
        pass
    else:
        raise AssertionError('invalid TTL accepted')
    assert not db._fetchall('SELECT * FROM premium_pass_tokens')
    """)


def test_fresh_repeated_and_existing_schema_migration(tmp_path):
    run(tmp_path, """
    token = ready()
    db.init_db()
    passes.init_schema()
    assert passes.pending_pass(UID)
    assert passes.redeem_pass(UID, token)['status'] == 'activated'
    db.init_db()
    assert passes.has_active_pass(UID)
    db._execute('DROP TABLE premium_pass_tokens')
    db._execute('DROP TABLE premium_pass_entitlements')
    # Simulates a populated database from before the feature existed.
    db.grant_premium(UID+1)
    db.init_db()
    assert db.is_user_premium(UID+1)
    assert ready()
    assert len(db._fetchall('SELECT * FROM premium_pass_tokens')) == 1
    """)


@pytest.mark.parametrize('days', [None, 30])
def test_paid_lifetime_untouched_and_revoke(tmp_path, days):
    run(tmp_path, f"""
    db.grant_premium(UID, days={days!r})
    before = dict(db._fetchone('SELECT is_premium, premium_until FROM user_settings WHERE user_id=?', (UID,)))
    token = ready()
    assert passes.redeem_pass(UID, token)['status'] == 'activated'
    passes.revoke_pass(UID)
    assert db.is_user_premium(UID)
    token = ready()
    assert passes.redeem_pass(UID, token)['status'] == 'activated'
    after = dict(db._fetchone('SELECT is_premium, premium_until FROM user_settings WHERE user_id=?', (UID,)))
    assert before == after
    db.revoke_premium(UID)
    assert not passes.has_active_pass(UID)
    assert not db.is_user_premium(UID)
    token = ready()
    db.revoke_premium(UID)
    assert passes.redeem_pass(UID, token)['status'] == 'invalid'
    """)


def test_premium_fallback_all_false_paths_and_admin(tmp_path):
    run(tmp_path, """
    with patch.object(passes, 'has_active_pass', return_value=True) as active:
        assert db.is_user_premium(UID)  # missing user row
        active.assert_called_once_with(UID)
        db.ensure_user_exists(UID)
        assert db.is_user_premium(UID)  # nonpremium row
        db._execute("UPDATE user_settings SET is_premium=1, premium_until='2000-01-01' WHERE user_id=?", (UID,))
        assert db.is_user_premium(UID)  # expired paid row
    with patch.object(passes, 'has_active_pass', side_effect=AssertionError('admin must short circuit')):
        assert db.is_user_premium(db.settings.admin_id)
        db.set_bot_setting('extra_admins', str(UID))
        assert db.is_user_premium(UID)
    """)


def test_duplicate_redemption_concurrent(tmp_path):
    run(tmp_path, """
    for _ in range(8):
        passes.revoke_pass(UID)
        token = ready()
        gate = threading.Barrier(8)
        def redeem(_):
            gate.wait()
            return passes.redeem_pass(UID, token)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(redeem, range(8)))
        assert sum(r['status'] == 'activated' for r in results) == 1
        assert sum(r['status'] == 'invalid' for r in results) == 7
        assert len(db._fetchall('SELECT * FROM premium_pass_entitlements')) == 1
    """)


def test_concurrent_issuance_only_current_token_can_redeem(tmp_path):
    run(tmp_path, """
    gate = threading.Barrier(8)
    def issue(_):
        gate.wait()
        return passes.issue_pass(UID)['token']
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(issue, range(8)))
    assert len(set(tokens)) == 8
    assert len(db._fetchall('SELECT * FROM premium_pass_tokens')) == 1
    successes = [t for t in tokens if passes.mark_pass_ready(UID, t, URL)]
    assert len(successes) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: passes.redeem_pass(UID, t), tokens))
    assert sum(r['status'] == 'activated' for r in results) == 1
    assert all(r['status'] in ('activated', 'invalid') for r in results)
    """)


def test_active_entitlement_prevents_stacking_stale_pending(tmp_path):
    run(tmp_path, """
    token = ready()
    until = int(passes.time.time())+4000
    db._execute('INSERT INTO premium_pass_entitlements VALUES (?, ?)', (UID, until))
    assert passes.redeem_pass(UID, token) == {'status': 'active', 'premium_until': until}
    assert not db._fetchall('SELECT * FROM premium_pass_tokens')
    assert passes.active_pass_until(UID) == until
    """)


def test_issue_redeem_race_never_stacks(tmp_path):
    run(tmp_path, """
    with patch.object(passes.time, 'time', return_value=NOW):
        for _ in range(12):
            passes.revoke_pass(UID)
            old = ready()
            gate = threading.Barrier(2)
            def redeem():
                gate.wait()
                return passes.redeem_pass(UID, old)
            def issue():
                gate.wait()
                return passes.issue_pass(UID)
            with ThreadPoolExecutor(max_workers=2) as pool:
                r = pool.submit(redeem)
                i = pool.submit(issue)
                redemption, issuance = r.result(), i.result()
            if redemption['status'] == 'activated':
                assert issuance == {'status': 'active', 'premium_until': NOW+86400}
            else:
                assert redemption['status'] == 'invalid'
                assert issuance['status'] == 'issued'
                assert passes.mark_pass_ready(UID, issuance['token'], URL)
                assert passes.redeem_pass(UID, issuance['token']) == {'status': 'activated', 'premium_until': NOW+86400}
            assert passes.active_pass_until(UID) == NOW+86400
    """)


def test_expired_reissue_stale_api_result_and_timezone_independence(tmp_path):
    run(tmp_path, """
    import os
    import time
    with patch.object(passes.time, 'time', return_value=NOW):
        old = passes.issue_pass(UID, ttl_seconds=1)['token']
    with patch.object(passes.time, 'time', return_value=NOW+1):
        token = passes.issue_pass(UID)['token']
        assert not passes.mark_pass_ready(UID, old, URL)
        passes.cancel_pass(UID, old)
        assert passes.mark_pass_ready(UID, token, URL)
        assert passes.redeem_pass(UID, token)['premium_until'] == NOW+1+86400
        for zone in ['UTC', 'Asia/Kolkata', 'America/New_York']:
            os.environ['TZ'] = zone
            time.tzset()
            assert passes.active_pass_until(UID) == NOW+1+86400
    """)


def test_sqlite_atomic_failure_rollback(tmp_path):
    run(tmp_path, """
    token = ready()
    db._execute("CREATE TRIGGER fail_consume BEFORE DELETE ON premium_pass_tokens BEGIN SELECT RAISE(ABORT, 'forced failure'); END")
    try:
        passes.redeem_pass(UID, token)
    except Exception as exc:
        assert 'forced failure' in str(exc)
    else:
        raise AssertionError('expected failure')
    assert not passes.has_active_pass(UID)
    assert passes.pending_pass(UID)
    db._execute('DROP TRIGGER fail_consume')
    assert passes.redeem_pass(UID, token)['status'] == 'activated'
    """)


@pytest.mark.parametrize('fail', [False, True])
def test_pg_transaction_lock_commit_rollback_and_close_mocked(tmp_path, fail):
    run(tmp_path, f"""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    # user lock, token read, entitlement read
    cur.fetchone.side_effect = [(UID,), (NOW+1800, 1), None]
    statements = []
    def execute(sql, params=None):
        assert conn.autocommit is False
        statements.append((sql, params))
        if {fail!r} and sql.startswith('DELETE FROM premium_pass_tokens'):
            raise RuntimeError('forced PG error')
    cur.execute.side_effect = execute
    with patch.object(db, '_using_pg', True), \\
         patch.object(db, 'ensure_user_exists') as ensure, \\
         patch.object(db, '_new_pg_connection', return_value=conn, create=True), \\
         patch.object(passes.time, 'time', return_value=NOW):
        try:
            result = passes.redeem_pass(UID, 'a'*32)
        except RuntimeError:
            assert {fail!r}
        else:
            assert not {fail!r}
            assert result == {{'status': 'activated', 'premium_until': NOW+86400}}
    ensure.assert_called_once_with(UID)
    assert statements[0][0].endswith('FOR UPDATE')
    assert statements[0][1] == (UID,)
    assert all('?' not in sql for sql, _ in statements)
    assert any('ON CONFLICT' in sql for sql, _ in statements)
    if {fail!r}:
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
    else:
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()
    conn.close.assert_called_once()
    """)


def test_pg_schema_and_named_read_results_mocked(tmp_path):
    run(tmp_path, """
    with patch.object(db, '_using_pg', True), patch.object(db, '_execute') as execute:
        passes.init_schema()
        passes.init_schema()
    assert execute.call_count == 4
    sql = '\\n'.join(call.args[0] for call in execute.call_args_list)
    assert 'user_id BIGINT' in sql
    assert 'premium_until BIGINT' in sql
    assert 'UNIQUE' in sql and 'IF NOT EXISTS' in sql
    with patch.object(db, '_using_pg', True), patch.object(db, '_fetchone') as fetch, \\
         patch.object(passes.time, 'time', return_value=NOW):
        fetch.return_value = {'premium_until': NOW+1}
        assert passes.has_active_pass(UID)
        fetch.return_value = {'shortened_url': URL, 'expires_at': NOW+20}
        assert passes.pending_pass(UID) == {'shortened_url': URL, 'expires_at': NOW+20}
        assert '%s' in fetch.call_args.args[0]
    """)
