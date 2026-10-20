"""Optional real PostgreSQL test, isolated in a synthetic schema.

Run with NODICK_TEST_POSTGRES_URL pointing to a disposable loopback server.
Never uses DATABASE_URL from the real application environment.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

import pytest


def test_real_postgres_atomic_pass_redemption():
    url = os.getenv('NODICK_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set NODICK_TEST_POSTGRES_URL to a disposable loopback PostgreSQL server')
    assert urlsplit(url).hostname in {'127.0.0.1', 'localhost', '::1'}, 'Only disposable loopback PostgreSQL is allowed'
    env = os.environ.copy()
    env['DATABASE_URL'] = url
    code = '''
import json,time,uuid
from concurrent.futures import ThreadPoolExecutor
from nodick import db,premium_passes as passes
schema='nodick_pass_test_'+uuid.uuid4().hex
setup=db._new_pg_connection();setup.autocommit=True
with setup.cursor() as cur:
    cur.execute('CREATE SCHEMA '+schema)
db._PG_CONNECT_KWARGS['options'] += ' -c search_path='+schema
try:
    db.init_db();db.init_db()
    issued=passes.issue_pass(101)
    assert passes.redeem_pass(101,issued['token'])['status']=='invalid'
    assert passes.mark_pass_ready(101,issued['token'],'https://links.example.com/example')
    assert passes.redeem_pass(102,issued['token'])['status']=='invalid'
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:passes.redeem_pass(101,issued['token']),range(6)))
    assert sum(r['status']=='activated' for r in results)==1
    assert db.is_user_premium(101)
    assert 86395 <= passes.active_pass_until(101)-int(time.time()) <=86400
    assert passes.redeem_pass(101,issued['token'])['status']=='invalid'
    for uid,days in ((103,None),(104,30)):
        db.grant_premium(uid,days)
        before=dict(db._fetchone('SELECT is_premium,premium_until FROM user_settings WHERE user_id=%s',(uid,)))
        token=passes.issue_pass(uid)['token']
        passes.mark_pass_ready(uid,token,'https://links.example.com/preserve')
        passes.redeem_pass(uid,token)
        assert before==dict(db._fetchone('SELECT is_premium,premium_until FROM user_settings WHERE user_id=%s',(uid,)))
    db.revoke_premium(101)
    assert not db.is_user_premium(101)
    print(json.dumps({'activated':1,'concurrent_attempts':6,'paid_and_lifetime_preserved':True}))
finally:
    if db._pg_pool:
        db._pg_pool.closeall()
    with setup.cursor() as cur:
        cur.execute('DROP SCHEMA '+schema+' CASCADE')
    setup.close()
'''
    result = subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],env=env,text=True,capture_output=True,check=True)
    assert json.loads(result.stdout) == {'activated':1,'concurrent_attempts':6,'paid_and_lifetime_preserved':True}
