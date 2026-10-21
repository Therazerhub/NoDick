"""Disposable SQLite checks for lifetime trials and referral day grants."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(tmp_path, script):
    env = os.environ.copy()
    env.pop('DATABASE_URL', None)
    env['DB_PATH'] = str(tmp_path / 'trial.db')
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_trial_lifetime_concurrency_and_legacy(tmp_path):
    result = run(tmp_path, '''
import json
from concurrent.futures import ThreadPoolExecutor
from nodick import db
# Old database rows and evidence from quota rollover must never regain a trial.
db.init_db()
db.ensure_user_exists(1)
db._execute("UPDATE user_settings SET quota_used=0, quota_date='2020-01-01' WHERE user_id=?", (1,))
db.init_db()
assert db.video_trial_status(1)['remaining'] == 0
with ThreadPoolExecutor(max_workers=8) as pool:
    raced = list(pool.map(lambda _: db.reserve_video_trial(2), range(8)))
assert sum(r['status'] == 'reserved' for r in raced) == 1
first = next(r for r in raced if r['status'] == 'reserved')
assert db.finish_video_trial(999, first['token'], True)['status'] == 'invalid'
assert db.finish_video_trial(2, first['token'], False)['status'] == 'released'
for expected in (2, 1, 0):
    token = db.reserve_video_trial(2)['token']
    assert db.finish_video_trial(2, token, True)['remaining'] == expected
    assert db.finish_video_trial(2, token, True)['status'] == 'invalid'
assert db.reserve_video_trial(2)['status'] == 'exhausted'
db.init_db()
print(json.dumps({'legacy': db.video_trial_status(1), 'new': db.video_trial_status(2)}))
''')
    assert result == {'legacy': {'remaining': 0}, 'new': {'remaining': 0}}


def test_referral_one_day_idempotent_and_paid_untouched(tmp_path):
    result = run(tmp_path, '''
import json, time
from concurrent.futures import ThreadPoolExecutor
from nodick import db
from nodick.premium_passes import active_pass_until
db.init_db()
db.ensure_user_exists(1)
def activate(i):
    db.ensure_user_exists(i)
    assert db.set_pending_referral(1, i)
    db.record_growth_event('first_delivery', i, video_id=1, first_only=True)
    return db.activate_pending_referral(i)
with ThreadPoolExecutor(max_workers=3) as pool:
    results = list(pool.map(activate, (2, 3, 4)))
assert sum(x['premium_awarded'] for x in results) == 1
assert sorted(x['premium_status'] for x in results) == ['active', 'active', 'granted']
until = active_pass_until(1)
assert 86390 <= until - int(time.time()) <= 86400
assert db.activate_pending_referral(2) is None
assert active_pass_until(1) == until
db.grant_premium(1)
paid = activate(5)
assert paid['premium_status'] == 'paid'
row = db._fetchone('SELECT is_premium, premium_until FROM user_settings WHERE user_id=?', (1,))
print(json.dumps({'paid': paid['premium_status'], 'premium': row['is_premium'], 'until': row['premium_until'], 'count': db.get_referral_count(1)}))
''')
    assert result == {'paid': 'paid', 'premium': 1, 'until': None, 'count': 4}
