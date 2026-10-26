"""Owner-local task quality policy, observed quota and Leader review queue.

No inference, credential access, reset redemption or task execution lives here.
Compatible with the frozen Python 3.6 Worker; routes are owner policy, not a
permanent model ranking or a claim of independent model quality verification.
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import uuid

from .model_recovery import (classify_failure, native_resume_compatible, qualified, requirements,
                             route_bindings, startable)


DIFFICULTIES = ('D1', 'D2', 'D3')
REVIEW_KINDS = ('integration', 'model_comparison', 'gui', 'fault_drill', 'acceptance')


class GovernanceError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))


def text(value, maximum=200):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and not any(ord(c) < 32 for c in value))


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def private_json(path):
    target = Path(path)
    entry = target.lstat()
    if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid()
            or entry.st_nlink != 1 or entry.st_mode & 0o077 or entry.st_size > 1024 * 1024):
        raise GovernanceError('governance_private_json_required')
    descriptor = os.open(str(target), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding='utf8') as handle:
        return json.load(handle)


def metadata(value):
    if (not isinstance(value, dict) or value.get('difficulty') not in DIFFICULTIES
            or not text(value.get('rationale'), 2000)
            or value.get('risk') not in ('low', 'medium', 'high')
            or not isinstance(value.get('priority'), int) or isinstance(value['priority'], bool)
            or not 0 <= value['priority'] <= 9
            or set(value) - {'difficulty', 'rationale', 'risk', 'priority', 'route_id', 'requirements'}
            or ('route_id' in value and not text(value['route_id']))):
        raise GovernanceError('governance_task_rating_required')
    try:
        requirements(value.get('requirements', {}))
    except ValueError as exc:
        raise GovernanceError(str(exc)) from None
    return dict(value)


def quota_snapshot(value, observed_at):
    """Project actual SDK data only; secondary is NOT assumed to be weekly."""
    bucket = None
    if isinstance(value, dict):
        buckets = value.get('rateLimitsByLimitId')
        if isinstance(buckets, dict):
            bucket = buckets.get('codex')
        else:
            bucket = value.get('rateLimits')
    if not isinstance(bucket, dict) or bucket.get('limitId') not in (None, 'codex'):
        bucket = {}
    result = {'observed_at': observed_at, 'bucket': 'codex', 'status': 'unknown', 'windows': []}
    known, stale, resets = False, False, []
    for name in ('primary', 'secondary'):
        raw = bucket.get(name)
        if not isinstance(raw, dict):
            continue
        percent, minutes, reset = raw.get('usedPercent'), raw.get('windowDurationMins'), raw.get('resetsAt')
        percent = percent if number(percent) and percent >= 0 else None
        minutes = minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0 else None
        reset = reset if number(reset) and reset > 0 else None
        result['windows'].append({'name': name, 'used_percent': percent,
            'window_duration_mins': minutes, 'resets_at': reset, 'weekly': minutes == 10080})
        if percent is not None:
            known = True
            if reset is not None and reset <= observed_at:
                stale = True
            if percent >= 100:
                if reset is not None and reset > observed_at:
                    resets.append(reset)
                else:
                    stale = True
    if resets:
        result.update(status='exhausted', retry_at=max(resets))
    elif known and not stale:
        result['status'] = 'available'
    return result


def policy_routes(policy):
    if (not isinstance(policy, dict) or not text(policy.get('revision'))
            or not isinstance(policy.get('routes'), list) or not policy['routes']
            or not isinstance(policy.get('defaults'), dict)):
        raise GovernanceError('governance_owner_policy_required')
    routes = {}
    for row in policy['routes']:
        if (not isinstance(row, dict) or not all(text(row.get(k)) for k in ('id', 'model', 'effort'))
                or row.get('harness') != 'codex'
                or row.get('cost_basis') not in ('included_subscription', 'local', 'free')
                or row.get('quality') not in ('standard', 'strong')
                or not isinstance(row.get('difficulties'), list) or not row['difficulties']
                or not set(row['difficulties']) <= set(DIFFICULTIES) or row['id'] in routes
                or any(k in row and not text(row[k]) for k in
                       ('provider', 'provider_identity', 'endpoint_id', 'account_id', 'egress_id',
                        'frontier_candidate_id', 'reasoning_mode'))
                or ('marginal_cost_usd' in row and (not number(row['marginal_cost_usd'])
                    or row['marginal_cost_usd'] < 0))
                or any(k in row and (not isinstance(row[k], list)
                    or not all(text(v) for v in row[k])) for k in ('capabilities', 'allowed_scopes'))):
            raise GovernanceError('governance_owner_route_invalid')
        routes[row['id']] = dict(row)
    if any(policy['defaults'].get(d) not in routes for d in DIFFICULTIES):
        raise GovernanceError('governance_default_route_missing')
    return routes


class Governance:
    def __init__(self, database, policy=None, clock=time.time):
        self.clock, self.policy = clock, policy
        self.routes = policy_routes(policy) if policy is not None else {}
        self.policy_hash = hashlib.sha256(canonical(policy).encode()).hexdigest() if policy else None
        self.path = Path(database)
        if not self.path.is_absolute() or '..' in self.path.parts:
            raise GovernanceError('governance_private_database_required')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for ancestor in (self.path.parent,) + tuple(self.path.parent.parents):
            entry = ancestor.lstat()
            if (not stat.S_ISDIR(entry.st_mode) or entry.st_uid not in (0, os.getuid())
                    or (entry.st_mode & 0o022 and not entry.st_mode & stat.S_ISVTX)):
                raise GovernanceError('governance_private_parent_required')
        if self.path.parent.stat().st_mode & 0o077:
            raise GovernanceError('governance_private_parent_required')
        descriptor = os.open(str(self.path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        entry = os.fstat(descriptor)
        os.close(descriptor)
        if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077 or entry.st_nlink != 1:
            raise GovernanceError('governance_private_database_required')
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS task_ratings(task_id TEXT PRIMARY KEY, revision INTEGER, value TEXT);
                CREATE TABLE IF NOT EXISTS governance_intents(id TEXT PRIMARY KEY, fingerprint TEXT);
                CREATE TABLE IF NOT EXISTS quota_observations(route_id TEXT, account_id TEXT,
                    value TEXT, PRIMARY KEY(route_id, account_id));
                CREATE TABLE IF NOT EXISTS governance_decisions(task_id TEXT, epoch INTEGER,
                    value TEXT, PRIMARY KEY(task_id, epoch));
                CREATE TABLE IF NOT EXISTS leader_review_queue(id TEXT PRIMARY KEY, fingerprint TEXT,
                    value TEXT, status TEXT, scheduled_at REAL, updated REAL);
                CREATE TABLE IF NOT EXISTS leader_review_plans(id TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS model_route_availability(route_key TEXT PRIMARY KEY,
                    identity TEXT, value TEXT);
                CREATE TABLE IF NOT EXISTS model_attempts(id TEXT PRIMARY KEY, task_id TEXT,
                    epoch INTEGER, route_key TEXT, value TEXT);
            ''')

    @contextlib.contextmanager
    def transaction(self):
        with contextlib.closing(sqlite3.connect(str(self.path), timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db

    def rate(self, task_id, value, request_id, expected_revision=None):
        value = metadata(value)
        if not text(task_id) or not text(request_id):
            raise GovernanceError('governance_stable_id_required')
        fingerprint = hashlib.sha256(canonical([task_id, value, expected_revision]).encode()).hexdigest()
        with self.transaction() as db:
            prior = db.execute('SELECT fingerprint FROM governance_intents WHERE id=?', (request_id,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise GovernanceError('governance_request_conflict')
                return dict(value, replayed=True)
            old = db.execute('SELECT revision FROM task_ratings WHERE task_id=?', (task_id,)).fetchone()
            if (old['revision'] if old else None) != expected_revision:
                raise GovernanceError('governance_rating_revision_conflict')
            revision = 1 if old is None else old['revision'] + 1
            db.execute('INSERT INTO governance_intents VALUES(?,?)', (request_id, fingerprint))
            db.execute('INSERT OR REPLACE INTO task_ratings VALUES(?,?,?)', (task_id, revision, canonical(value)))
        return dict(value, revision=revision)

    def select_route(self, rating, role_profile=None):
        routes = self.candidate_routes(rating, role_profile)
        if not routes:
            raise GovernanceError('governance_advanced_quality_floor' if rating['difficulty'] == 'D3'
                                  else 'governance_qualified_route_required')
        return dict(routes[0])

    def candidate_routes(self, rating, role_profile=None):
        """Owner defaults/profiles are preferences, not permanent model gates."""
        if role_profile is not None:
            if (not isinstance(role_profile, dict) or not text(role_profile.get('model'))
                    or not text(role_profile.get('reasoning_effort'))):
                raise GovernanceError('governance_role_profile_incomplete')
        preferred = rating.get('route_id') or (self.policy or {}).get('defaults', {}).get(rating['difficulty'])
        candidates = [r for r in self.routes.values() if rating['difficulty'] in r['difficulties']
                      and (rating['difficulty'] != 'D3' or r['quality'] == 'strong')]
        return sorted(candidates, key=lambda r: (
            not (role_profile and r['model'] == role_profile['model']
                 and r['effort'] == role_profile['reasoning_effort']),
            r['id'] != preferred, r.get('marginal_cost_usd', 0), r['id']))

    def task_rating(self, task, persist=False):
        with self.transaction() as db:
            row = db.execute('SELECT value FROM task_ratings WHERE task_id=?', (task['id'],)).fetchone()
        raw = json.loads(row['value']) if row else (task.get('context') or {}).get('governance')
        rating = metadata(raw)
        if persist and not row:
            self.rate(task['id'], rating, task['id'] + ':initial-governance')
        return rating

    def choose(self, task, role_profile=None):
        if not startable(task):
            raise GovernanceError('governance_original_task_not_safely_startable')
        rating = self.task_rating(task, persist=True)
        route = self.select_route(rating, role_profile)
        return {'rating': rating, 'route': dict(route), 'policy_revision': self.policy['revision'],
                'policy_sha256': self.policy_hash, 'quota_policy': 'wait_for_qualified_route',
                'selection_basis': 'owner_role_profile' if role_profile is not None else 'task_route_or_default',
                'automatic_free_or_pi_fallback': False, 'independent_quality_verified': False,
                'qualified_free_routes_allowed': True, 'model_outage_switches_harness': False}

    def recovery_plan(self, task, config, role_profile=None, _bindings=False):
        """Local supervisor projection. No account/model RPC, claim or replay.

        next_recheck_at is a scheduling hint for the ORIGINAL authority task;
        callers must still obtain the ordinary claim/epoch fence. A due failure
        becomes probeable, not proven healthy. Internal bindings contain only
        private config paths; public projections omit them.
        """
        now = self.clock()
        result = dict(task_id=task['id'], epoch=task.get('epoch'), status='waiting_model',
            qualified_routes=[], blocked_routes=[], next_recheck_at=None,
            rescue_harness_allowed=False, automatic_task_replay=False,
            model_outage_switches_harness=False, network_ready_independent=True,
            actual_model_effort_verified=False)
        if not startable(task):
            return dict(result, status='hold_original_operation')
        try:
            rating = self.task_rating(task)
            candidates = self.candidate_routes(rating, role_profile)
        except GovernanceError as exc:
            return dict(result, status='waiting_policy', reason=str(exc))
        result['rating'] = rating
        advice = None
        if any(r.get('frontier_candidate_id') for r in candidates):
            from .model_advice import model_advice
            needs = dict(rating.get('requirements', {}))
            needs.setdefault('data_scope', 'owner_private')
            advice = model_advice(config, needs, now=now)
        with self.transaction() as db:
            availability = {r['route_key']: json.loads(r['value']) for r in
                db.execute('SELECT route_key,value FROM model_route_availability')}
        quotas = {(q['route_id'], q['account_id']): q for q in self.quotas()}
        retry_times, bindings, failures = [], [], []
        for route in candidates:
            found = False
            try:
                for binding in route_bindings(config, route, rejected=result['blocked_routes']):
                    found = True
                    identity = binding['identity']
                    accepted, basis = qualified(route, rating, identity, now, advice)
                    if not accepted:
                        result['blocked_routes'].append(dict(identity, reason=basis))
                        continue
                    compatible, reason = native_resume_compatible(task, binding, config)
                    if not compatible:
                        result['blocked_routes'].append(dict(identity, reason=reason))
                        continue
                    observation = availability.get(identity['route_key'])
                    quota = quotas.get((route['id'], identity['account_id']))
                    if quota and quota['current_status'] == 'exhausted':
                        observation = dict(category='quota', reason='model_route_quota',
                            retry_at=quota['observation']['retry_at'],
                            observed_at=quota['observation']['observed_at'])
                    if observation and observation.get('retry_at', 0) > now:
                        failures.append(observation['category'])
                        retry_times.append(observation['retry_at'])
                        result['blocked_routes'].append(dict(identity, **observation))
                        continue
                    result['qualified_routes'].append(dict(identity, quality_basis=basis,
                        quota_status=quota['current_status'] if quota else 'unknown',
                        availability='retry_due' if observation else 'not_known_unavailable'))
                    binding['quality_basis'] = basis
                    bindings.append(binding)
            except (OSError, ValueError, KeyError, TypeError):
                result['blocked_routes'].append(dict(route_id=route['id'],
                    reason='route_configuration_or_cost_invalid'))
            if not found:
                result['blocked_routes'].append(dict(route_id=route['id'], reason='route_not_installed'))
        if bindings:
            result['status'] = 'ready'
        # No inference health loop: supervisors sleep until this deadline or a
        # capability/auth/network/config observation signals an earlier change.
        result['next_recheck_at'] = min(retry_times) if retry_times else (None if bindings else now + 300)
        result['rescue_harness_allowed'] = not bindings and bool(failures) and all(f == 'framework' for f in failures)
        if _bindings:
            result['_bindings'] = bindings
        return result

    def route_failure(self, identity, error, phase='preflight', protocol=None, retry_at=None):
        value = classify_failure(error, self.clock(), phase, protocol, retry_at)
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO model_route_availability VALUES(?,?,?)',
                (identity['route_key'], canonical(identity), canonical(value)))
        return value

    def route_available(self, identity):
        with self.transaction() as db:
            db.execute('DELETE FROM model_route_availability WHERE route_key=?', (identity['route_key'],))

    def begin_attempt(self, task, identity):
        attempt_id = str(uuid.uuid4())
        value = dict(attempt_id=attempt_id, task_id=task['id'], epoch=task['epoch'],
            route=identity, status='preflight', native_start_attempted=False,
            operation_id=(task.get('checkpoint') or {}).get('operation_id') or
                         (task.get('context') or {}).get('operation_id'),
            observed_at=self.clock(), automatic_operation_replay=False)
        with self.transaction() as db:
            db.execute('INSERT INTO model_attempts VALUES(?,?,?,?,?)',
                (attempt_id, task['id'], task['epoch'], identity['route_key'], canonical(value)))
        return attempt_id

    def update_attempt(self, attempt_id, **changes):
        if set(changes) - {'status', 'native_start_attempted', 'failure', 'outcome', 'thread_id', 'turn_id'}:
            raise GovernanceError('governance_model_attempt_fields_invalid')
        with self.transaction() as db:
            row = db.execute('SELECT value FROM model_attempts WHERE id=?', (attempt_id,)).fetchone()
            value = dict(json.loads(row['value']), **changes)
            value['observed_at'] = self.clock()
            db.execute('UPDATE model_attempts SET value=? WHERE id=?', (canonical(value), attempt_id))
        return value

    def attempts(self, task_id):
        with self.transaction() as db:
            return [json.loads(row['value']) for row in db.execute(
                'SELECT value FROM model_attempts WHERE task_id=? ORDER BY rowid', (task_id,))]

    def observe(self, route_id, account_id, raw):
        value = quota_snapshot(raw, self.clock())
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO quota_observations VALUES(?,?,?)',
                       (route_id, account_id, canonical(value)))
            if value['status'] == 'available':
                for row in db.execute('SELECT route_key,identity,value FROM model_route_availability').fetchall():
                    identity, failure = json.loads(row['identity']), json.loads(row['value'])
                    if (identity['route_id'] == route_id and identity['account_id'] == account_id
                            and failure.get('category') == 'quota'):
                        db.execute('DELETE FROM model_route_availability WHERE route_key=?', (row['route_key'],))
        return value

    def record_decision(self, task, value):
        value = dict(value, task_id=task['id'], epoch=task['epoch'], observed_at=self.clock(),
                     automatic_task_replay=False,
                     preflight_observation_only=not value.get('native_start_attempted', False))
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO governance_decisions VALUES(?,?,?)',
                       (task['id'], task['epoch'], canonical(value)))
        return value

    def decision(self, task_id, epoch):
        with self.transaction() as db:
            row = db.execute('SELECT value FROM governance_decisions WHERE task_id=? AND epoch=?',
                             (task_id, epoch)).fetchone()
        return json.loads(row['value']) if row else None

    def quotas(self):
        with self.transaction() as db:
            result = []
            for row in db.execute('SELECT route_id,account_id,value FROM quota_observations'):
                snapshot = json.loads(row['value'])
                fresh = 0 <= self.clock() - snapshot['observed_at'] <= 300
                expired = any(number(w['resets_at']) and w['resets_at'] <= self.clock()
                              for w in snapshot['windows'])
                result.append({'route_id': row['route_id'], 'account_id': row['account_id'],
                    'observation': snapshot, 'current_status': snapshot['status'] if fresh and not expired else 'unknown'})
            return result

    def enqueue_review(self, review_id, value):
        required = {'project_id', 'task_id', 'kind', 'code_revision', 'operation_id',
                    'required_nodes', 'model', 'owner_cooperation', 'quota_budget'}
        if (not text(review_id) or not isinstance(value, dict) or set(value) != required
                or value['kind'] not in REVIEW_KINDS
                or not all(text(value[k]) for k in ('project_id', 'task_id', 'code_revision', 'operation_id', 'model'))
                or not isinstance(value['required_nodes'], list)
                or not all(text(n) for n in value['required_nodes'])
                or not isinstance(value['owner_cooperation'], bool)
                or not text(value['quota_budget'], 1000)):
            raise GovernanceError('governance_review_metadata_required')
        digest = hashlib.sha256(canonical(value).encode()).hexdigest()
        with self.transaction() as db:
            old = db.execute('SELECT fingerprint FROM leader_review_queue WHERE id=?', (review_id,)).fetchone()
            if old and old['fingerprint'] != digest:
                raise GovernanceError('governance_review_id_conflict')
            db.execute('INSERT OR IGNORE INTO leader_review_queue VALUES(?,?,?,\'queued\',NULL,?)',
                       (review_id, digest, canonical(value), self.clock()))
        return {'id': review_id, 'replayed': old is not None, 'execution_authorized': False}

    def reviews(self):
        with self.transaction() as db:
            return [dict(row, value=json.loads(row['value']), plan=json.loads(row['plan']) if row['plan'] else None)
                    for row in db.execute('SELECT q.id,q.value,q.status,q.scheduled_at,q.updated,p.value AS plan '
                        'FROM leader_review_queue q LEFT JOIN leader_review_plans p ON p.id=q.id ORDER BY q.updated')]

    def schedule_reviews(self, leader_id, resources_available):
        if not text(leader_id) or not isinstance(resources_available, bool):
            raise GovernanceError('governance_leader_schedule_required')
        now = self.clock()
        with self.transaction() as db:
            pending = db.execute('SELECT id FROM leader_review_queue WHERE status=\'queued\'').fetchall()
            if not pending or not resources_available:
                return {'scheduled': 0, 'reason': 'no_pending_work_or_resources', 'execution_authorized': False}
            resets = []
            for row in db.execute('SELECT value FROM quota_observations'):
                snapshot = json.loads(row['value'])
                if 0 <= now - snapshot['observed_at'] <= 86400:
                    resets.extend(w['resets_at'] for w in snapshot['windows']
                                  if w['weekly'] and number(w['resets_at']) and w['resets_at'] > now)
            if resets:
                when, basis = max(now, min(resets) - 86400), 'observed_weekly_reset_minus_24h'
            else:
                zone = datetime.timezone(datetime.timedelta(hours=8), 'Asia/Shanghai')
                today = datetime.datetime.fromtimestamp(now, zone)
                target = (today + datetime.timedelta(days=(6 - today.weekday()) % 7)).replace(
                    hour=20, minute=0, second=0, microsecond=0)
                if target.timestamp() <= now:
                    target += datetime.timedelta(days=7)
                when, basis = target.timestamp(), 'Sunday20:00_Asia/Shanghai_no_reliable_weekly_window'
            db.execute('UPDATE leader_review_queue SET status=\'scheduled\',scheduled_at=?,updated=? WHERE status=\'queued\'',
                       (when, now))
            for item in pending:
                db.execute('INSERT OR REPLACE INTO leader_review_plans VALUES(?,?)',
                    (item['id'], canonical({'leader_id': leader_id, 'basis': basis, 'scheduled_at': when,
                        'planned_at': now, 'execution_authorized': False})))
        return {'scheduled': len(pending), 'scheduled_at': when, 'basis': basis, 'leader_id': leader_id,
                'execution_authorized': False, 'requires_actual_mesh_execution_fence': True}

    def cancel_review(self, review_id):
        with self.transaction() as db:
            changed = db.execute('UPDATE leader_review_queue SET status=\'cancelled\',updated=? '
                'WHERE id=? AND status IN (\'queued\',\'scheduled\')', (self.clock(), review_id)).rowcount
        return {'cancelled': bool(changed), 'execution_authorized': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--policy', help='Owner-private route JSON, never credentials')
    commands = parser.add_subparsers(dest='action')
    item = commands.add_parser('rate')
    item.add_argument('--task-id', required=True)
    item.add_argument('--request-id', required=True)
    item.add_argument('--metadata-file', required=True)
    item.add_argument('--expected-revision', type=int)
    item = commands.add_parser('review-enqueue')
    item.add_argument('--id', required=True)
    item.add_argument('--metadata-file', required=True)
    commands.add_parser('review-list')
    commands.add_parser('quota-list')
    item = commands.add_parser('decision')
    item.add_argument('--task-id', required=True)
    item.add_argument('--epoch', type=int, required=True)
    item = commands.add_parser('review-schedule')
    item.add_argument('--leader-id', required=True)
    item.add_argument('--resources-available', action='store_true')
    item = commands.add_parser('review-cancel')
    item.add_argument('--id', required=True)
    args = parser.parse_args(argv)
    try:
        state = Governance(args.database, private_json(args.policy) if args.policy else None)
        if args.action == 'rate':
            value = state.rate(args.task_id, private_json(args.metadata_file), args.request_id, args.expected_revision)
        elif args.action == 'review-enqueue':
            value = state.enqueue_review(args.id, private_json(args.metadata_file))
        elif args.action == 'review-list':
            value = state.reviews()
        elif args.action == 'quota-list':
            value = state.quotas()
        elif args.action == 'decision':
            value = state.decision(args.task_id, args.epoch)
        elif args.action == 'review-schedule':
            value = state.schedule_reviews(args.leader_id, args.resources_available)
        elif args.action == 'review-cancel':
            value = state.cancel_review(args.id)
        else:
            parser.error('choose rate or review-*')
        print(canonical(value))
        return 0
    except (GovernanceError, OSError, sqlite3.Error, ValueError):
        print(canonical({'error': 'governance_request_denied', 'automatic_retry': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
