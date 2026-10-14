"""Durable, read-only frontier observations for a continuous Mesh Leader.

This is one optional local adapter, not the cross-platform Mesh runtime. Public
GETs, reviewed local metadata and advice publication are its only actions. No
model inference, credentials, package execution, native history or timer changes
are performed. Source text is untrusted reference data, never an instruction.
"""
import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .model_frontier import advise_models
from .provider import _checked_path
from .provider_runtime import _fingerprint, _owner_bytes, _unique_object


DAY = 86400
MAX_SOURCE_BYTES = 4 * 1024 * 1024
CATEGORIES = frozenset(('models', 'harness', 'skills', 'protocol'))
SAFETY = dict(model_calls=0, new_spend_authorized=False, native_features_restricted=False,
              live_dispatch_authorized=False, operation_replayed=False,
              automatic_upgrade_started=False)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', value):
        raise ValueError('frontier_identity_invalid')
    return value


def finite(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('frontier_duration_invalid')
    return value


def public_url(value):
    if not isinstance(value, str) or len(value) > 1024 or re.search(r'[\x00-\x20\x7f]', value):
        raise ValueError('frontier_public_source_invalid')
    try:
        url = urlsplit(value)
        valid = (url.scheme == 'https' and url.hostname and '.' in url.hostname
                 and re.fullmatch(r'[A-Za-z0-9.-]+', url.hostname)
                 and url.port in (None, 443) and not url.username and not url.password
                 and not url.query and not url.fragment
                 and not re.fullmatch(r'[0-9.]+', url.hostname))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('frontier_public_source_invalid')
    return value


def configuration(value):
    fields = {'schema', 'schedule_id', 'task_context', 'state_dir', 'sources',
              'interval_seconds', 'retry_seconds', 'timeout_seconds', 'proxy_url',
              'local_metadata', 'baseline_evidence_file'}
    if (not isinstance(value, dict) or set(value) - fields
            or type(value.get('schema')) is not int or value['schema'] != 1):
        raise ValueError('frontier_configuration_invalid')
    context = value.get('task_context')
    if (not isinstance(context, dict) or set(context) != {'goal_ref', 'task_id', 'project_id'}):
        raise ValueError('frontier_original_context_required')
    result = dict(value)
    result['schedule_id'] = identifier(value.get('schedule_id'))
    result['task_context'] = {key: identifier(item) for key, item in context.items()}
    result['interval_seconds'] = finite(value.get('interval_seconds', 4 * DAY), 3 * DAY, 5 * DAY)
    result['retry_seconds'] = finite(value.get('retry_seconds', 3600), 60, DAY)
    result['timeout_seconds'] = finite(value.get('timeout_seconds', 15), 0.1, 30)
    path = Path(value.get('state_dir', ''))
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('frontier_private_state_required')
    result['state_dir'] = str(path)
    sources = value.get('sources')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        raise ValueError('frontier_sources_required')
    result['sources'] = []
    seen = set()
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {'id', 'category', 'url', 'required'}
                or source['category'] not in CATEGORIES or type(source['required']) is not bool
                or source['id'] in seen):
            raise ValueError('frontier_source_invalid')
        seen.add(identifier(source['id']))
        result['sources'].append(dict(source, url=public_url(source['url'])))
    if {s['category'] for s in sources if s['required']} != CATEGORIES:
        raise ValueError('frontier_four_source_categories_required')
    proxy = value.get('proxy_url')
    if proxy is not None:
        try:
            item = urlsplit(proxy)
            valid = (item.scheme == 'http' and item.hostname in ('127.0.0.1', 'localhost', '::1')
                     and item.port and not item.username and not item.password
                     and item.path in ('', '/') and not item.query and not item.fragment)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError('frontier_child_proxy_invalid')
    result['proxy_url'] = proxy
    result['local_metadata'] = []
    metadata = value.get('local_metadata', [])
    if not isinstance(metadata, list) or len(metadata) > 32:
        raise ValueError('frontier_local_metadata_invalid')
    for item in metadata:
        if (not isinstance(item, dict) or set(item) - {'id', 'kind', 'path', 'cash_policy'}
                or not {'id', 'kind', 'path'} <= set(item)
                or item['kind'] not in ('codex_catalog', 'codex_config', 'file_digest')
                or item.get('cash_policy', 'unknown') not in
                    ('unknown', 'existing_subscription', 'free_api', 'local')
                or item['id'] in seen):
            raise ValueError('frontier_local_metadata_invalid')
        seen.add(identifier(item['id']))
        local = Path(item['path'])
        if not local.is_absolute() or '..' in local.parts or local.name in ('auth.json', 'credentials.json'):
            raise ValueError('frontier_no_credential_source')
        result['local_metadata'].append(dict(item))
    baseline = value.get('baseline_evidence_file')
    if baseline is not None and (not isinstance(baseline, str) or not Path(baseline).is_absolute()):
        raise ValueError('frontier_baseline_invalid')
    return result


def private_dir(path, create=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    entry = path.lstat()
    if (not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid()
            or stat.S_IMODE(entry.st_mode) != 0o700 or str(path.resolve()) != str(path)):
        raise ValueError('frontier_private_state_required')
    return path


def publish(path, data):
    """Immutable content-addressed publication; never truncate old evidence."""
    path = Path(path)
    if path.exists():
        if _owner_bytes(path, MAX_SOURCE_BYTES + 1, 'frontier_artifact_invalid') != data:
            raise ValueError('frontier_artifact_conflict')
        return
    temp = path.parent / ('pending-' + os.urandom(12).hex())
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(str(temp), str(path))  # EXCL: an old publication is never replaced.
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(str(temp))
        # A confirmed ledger record must not outlive an unpersisted unlink of
        # this temporary hard link (private readers require nlink == 1).
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, request, response, code, message, headers, url):
        public_url(url)
        if urlsplit(url).hostname not in self.hosts:
            raise ValueError('frontier_redirect_not_configured')
        return super().redirect_request(request, response, code, message, headers, url)


def fetch_public(source, config):
    """No ambient auth/cookies or model call; proxy belongs only to this opener."""
    proxies = {} if config['proxy_url'] is None else {'https': config['proxy_url']}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies),
        Redirects({urlsplit(s['url']).hostname for s in config['sources']}))
    request = urllib.request.Request(source['url'], headers={
        'User-Agent': 'personal-assistant-mesh-frontier/1 (read-only public reference)',
        'Accept': 'application/json, text/markdown, text/html;q=0.9, text/plain;q=0.8'})
    with opener.open(request, timeout=config['timeout_seconds']) as response:
        final = public_url(response.geturl())
        data = response.read(MAX_SOURCE_BYTES + 1)
        if response.getcode() != 200 or len(data) > MAX_SOURCE_BYTES:
            raise ValueError('frontier_response_not_complete')
        return data, {'http_status': 200, 'final_url': final,
                      'content_type': response.headers.get('Content-Type', '')[:128]}


class Headings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.active, self.text, self.document_started = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag in ('title', 'h1', 'h2', 'h3'):
            self.active, self.text = tag, []

    def handle_data(self, data):
        if self.active:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == self.active:
            value = ' '.join(' '.join(self.text).split())[:180]
            if tag == 'h1' and not self.document_started:
                # Navigation can precede the actual document by hundreds of
                # headings. Do not let it consume the entire short projection.
                self.parts = self.parts[:1] if self.parts else []
                self.document_started = True
            if (value and value not in self.parts and len(self.parts) < 16
                    and (tag == 'title' or self.document_started)):
                self.parts.append(value)
            self.active = None


def summarize(data):
    """Small navigational data only; full source is privately retained by SHA."""
    text = data.decode('utf8', errors='replace')
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except ValueError:
        value = None
    if isinstance(value, list) and value and isinstance(value[0], dict):
        value = value[0]
    if isinstance(value, dict):
        output = {key: value[key][:180] for key in ('tag_name', 'name', 'sha', 'published_at', 'html_url')
                  if isinstance(value.get(key), str)}
        commit = value.get('commit', {})
        if isinstance(commit, dict) and isinstance(commit.get('message'), str):
            output['subject'] = commit['message'].split('\n', 1)[0][:180]
        return output
    if '<html' in text[:2000].lower() or '<!doctype' in text[:2000].lower():
        parser = Headings()
        parser.feed(text)
        return {'headings': parser.parts}
    return {'headings': [line.lstrip('# ').strip()[:180] for line in text.splitlines()
                         if line.startswith('#')][:16]}


def metadata_observation(item, now):
    """Read selected non-secret fields; do not run wrappers or read auth files."""
    path = Path(item['path'])
    if str(path.resolve()) != str(path):
        raise ValueError('frontier_metadata_symlink')
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022 or before.st_nlink != 1
                or _fingerprint(before) != _fingerprint(path.lstat())):
            raise ValueError('frontier_metadata_unsafe')
        data = handle.read(MAX_SOURCE_BYTES + 1)
        if (len(data) > MAX_SOURCE_BYTES or _fingerprint(before) != _fingerprint(path.lstat())
                or _fingerprint(before) != _fingerprint(os.fstat(handle.fileno()))):
            raise ValueError('frontier_metadata_changed')
    result = dict(id=item['id'], kind=item['kind'], status='observed_metadata_only',
                  observed_at=now, file_mtime=before.st_mtime, bytes=len(data),
                  sha256=hashlib.sha256(data).hexdigest(), inference_available_verified=False,
                  quota_verified=False, cash_policy=item.get('cash_policy', 'unknown'))
    if item['kind'] == 'codex_config':
        # Exact harmless TOML scalars only. No env/key/base_url or arbitrary lines.
        result['configured'] = {}
        for key in ('model', 'model_reasoning_effort', 'model_provider'):
            found = re.findall(r'^' + key + r'\s*=\s*"([A-Za-z0-9_.:/-]{1,128})"\s*$',
                               data.decode('utf8'), re.MULTILINE)
            if len(found) == 1:
                result['configured'][key] = found[0]
    elif item['kind'] == 'codex_catalog':
        value = json.loads(data.decode('utf8'), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or not isinstance(value.get('models'), list):
            raise ValueError('frontier_catalog_invalid')
        result['catalog_fetched_at'] = value.get('fetched_at') if isinstance(value.get('fetched_at'), str) else None
        result['client_version'] = value.get('client_version') if isinstance(value.get('client_version'), str) else None
        result['models'] = []
        for row in value['models'][:256]:
            if not isinstance(row, dict) or not isinstance(row.get('slug'), str):
                continue
            slug = row['slug']
            if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}', slug):
                continue
            efforts = [level['effort'] for level in row.get('supported_reasoning_levels', [])
                       if isinstance(level, dict) and isinstance(level.get('effort'), str)
                       and re.fullmatch(r'[A-Za-z0-9_-]{1,32}', level['effort'])]
            result['models'].append({'model': slug, 'efforts': efforts[:16]})
        result['models_truncated'] = len(value['models']) > 256
    return result


def model_evidence(config, sources, metadata, now):
    baseline = config.get('baseline_evidence_file')
    if baseline is not None:
        raw = _owner_bytes(baseline, 1024 * 1024, 'frontier_baseline_invalid')
        value = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {'schema', 'snapshot', 'candidates', 'requirements', 'policy'}
                or type(value['schema']) is not int or value['schema'] != 1
                or len(value.get('candidates', [])) > 256 or len(value.get('snapshot', {}).get('points', [])) > 256):
            raise ValueError('frontier_baseline_invalid')
        advise_models(value['snapshot'], value['candidates'], value['requirements'], value['policy'], now=now)
        return value  # Every original observation time, cost and quality assumption remains.
    first = next(s for s in sources if s['category'] == 'models')
    candidates = []
    for item in metadata:
        for row in item.get('models', []):
            for effort in row['efforts'] or ['not_exposed']:
                if len(candidates) >= 256:
                    break
                identity = '{}:{}:{}'.format(item['id'], row['model'], effort)
                ref = 'local-metadata:' + item['sha256']
                observation = dict(verified=False, evidence_ref=ref, observed_at=item['observed_at'],
                                   expires_at=item['observed_at'] + config['interval_seconds'])
                policy = item['cash_policy']
                cost = dict(observation, policy=policy, effective_cost_usd=0 if policy != 'unknown' else None,
                            authorized=False, quota_available=False, paid_fallback=False, auto_reload=False)
                candidates.append(dict(candidate_id=identity, benchmark_point_id=None,
                    pair=dict(provider='codex', endpoint_id=item['id'], model=row['model'],
                              reasoning_effort=effort, reasoning_mode='catalog_unverified', fallback_mode='not_evaluated'),
                    availability=dict(observation, available=False), cost=cost, capabilities=[],
                    privacy=dict(observation, allowed_scopes=[])))
    snapshot = dict(schema=1, snapshot_id='unmeasured-public-catalog-' + str(int(now)),
        source_url=first['url'], methodology_version='no-numeric-benchmark-imported',
        comparison_set='unmeasured-catalog', cost_basis='unknown-task-cost',
        evidence_ref=first.get('evidence_ref', 'frontier:source-unavailable'), observed_at=now, points=[])
    return dict(schema=1, snapshot=snapshot, candidates=candidates, requirements={}, policy={})


def safe_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return 'public_http_' + str(error.code)
    if isinstance(error, (OSError, urllib.error.URLError, TimeoutError)):
        return 'public_source_unavailable'
    return 'source_or_metadata_invalid'


class FrontierRefresh:
    def __init__(self, config, fetch=fetch_public, clock=time.time):
        self.config, self.fetch, self.clock = configuration(config), fetch, clock
        self.root = Path(self.config['state_dir'])
        self.database = self.root / 'frontier.sqlite'
        self.namespace = digest({'schedule_id': self.config['schedule_id'], 'task_context': self.config['task_context']})

    @contextlib.contextmanager
    def db(self, write=False):
        if write:
            private_dir(self.root, create=True)
            _checked_path(self.database, create=True)
            db = sqlite3.connect(str(self.database), timeout=5, isolation_level=None)
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS namespace(id TEXT PRIMARY KEY)')
            old = db.execute('SELECT id FROM namespace').fetchall()
            if old and old != [(self.namespace,)]:
                db.close()
                raise ValueError('frontier_original_context_conflict')
            db.execute('INSERT OR IGNORE INTO namespace VALUES(?)', (self.namespace,))
            db.execute('''CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,input_sha TEXT NOT NULL,
                created REAL NOT NULL,status TEXT NOT NULL,receipt TEXT,next_due REAL,retry_at REAL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS observations(run TEXT NOT NULL,source TEXT NOT NULL,
                attempt INTEGER NOT NULL,value TEXT NOT NULL,PRIMARY KEY(run,source,attempt))''')
        else:
            private_dir(self.root)
            _checked_path(self.database)
            db = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=5, isolation_level=None)
            db.execute('BEGIN')
            if db.execute('SELECT id FROM namespace').fetchall() != [(self.namespace,)]:
                db.close()
                raise ValueError('frontier_original_context_conflict')
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def status(self):
        now = self.clock()
        base = dict(SAFETY, schema=1, task_context=self.config['task_context'],
                    schedule_id=self.config['schedule_id'], observed_at=now, interval_seconds=self.config['interval_seconds'])
        if not self.database.exists():
            return dict(base, status='not_started', due=True, next_due_at=None, request_id=None)
        with self.db() as db:
            row = db.execute('SELECT * FROM runs ORDER BY created DESC,rowid DESC LIMIT 1').fetchone()
        if row is None:
            return dict(base, status='not_started', due=True, next_due_at=None, request_id=None)
        when = row['next_due'] if row['status'] == 'completed' else row['retry_at']
        return dict(base, status=row['status'], request_id=row['id'], next_due_at=row['next_due'],
                    retry_at=row['retry_at'], due=when is None or now >= when,
                    receipt_available=row['receipt'] is not None)

    def receipt(self, request_id):
        identifier(request_id)
        with self.db() as db:
            row = db.execute('SELECT receipt FROM runs WHERE id=?', (request_id,)).fetchone()
        if row is None or row['receipt'] is None:
            return dict(SAFETY, status='pending_or_not_found', request_id=request_id)
        value = json.loads(row['receipt'], object_pairs_hook=_unique_object)
        expected = value.pop('receipt_sha256')
        if digest(value) != expected:
            raise ValueError('frontier_receipt_invalid')
        value['receipt_sha256'] = expected
        return value

    def run_due(self):
        status = self.status()
        if not status['due']:
            return status
        if status['status'] in ('collecting', 'partial'):
            request_id = status['request_id']  # Resume original read-only intent, never a model/upgrade.
        else:
            slot = 'initial' if status['next_due_at'] is None else str(int(status['next_due_at']))
            request_id = self.config['schedule_id'] + ':' + slot
        return self.refresh(request_id)

    def refresh(self, request_id):
        identifier(request_id)
        private_dir(self.root, create=True)
        lock = self.root / 'refresh.lock'
        _checked_path(lock, create=True)
        # A local adapter process lock, not distributed Leader authority.
        import fcntl
        descriptor = os.open(str(lock), os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(SAFETY, status='local_refresh_running', request_id=request_id)
            return self._refresh(request_id)
        finally:
            os.close(descriptor)

    def _refresh(self, request_id):
        config_sha = digest(self.config)
        now = self.clock()
        with self.db(write=True) as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (request_id,)).fetchone()
            if row and row['input_sha'] != config_sha:
                raise ValueError('frontier_request_content_conflict')
            if row and row['status'] == 'completed':
                return json.loads(row['receipt'], object_pairs_hook=_unique_object)
            db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,\'collecting\',NULL,NULL,NULL)',
                       (request_id, config_sha, now))
            db.execute('UPDATE runs SET status=\'collecting\',retry_at=NULL WHERE id=?', (request_id,))
        blobs = private_dir(self.root / 'blobs', create=True)
        observed = []
        for source in self.config['sources']:
            with self.db() as db:
                prior = db.execute('SELECT attempt,value FROM observations WHERE run=? AND source=? ORDER BY attempt DESC LIMIT 1',
                                   (request_id, source['id'])).fetchone()
            previous = None if prior is None else json.loads(prior['value'])
            if previous and previous['status'] == 'observed' and self.clock() < previous['expires_at']:
                data = _owner_bytes(blobs / (previous['sha256'] + '.body'), MAX_SOURCE_BYTES, 'frontier_source_invalid')
                if hashlib.sha256(data).hexdigest() != previous['sha256']:
                    raise ValueError('frontier_source_invalid')
                observed.append(previous)
                continue
            attempt = 1 if prior is None else prior['attempt'] + 1
            start = dict(source, attempt=attempt, status='fetching', observed_at=self.clock())
            with self.db(write=True) as db:
                db.execute('INSERT INTO observations VALUES(?,?,?,?)', (request_id, source['id'], attempt, encoded(start).decode()))
            record = dict(start)
            try:
                data, transport = self.fetch(source, self.config)
                if not isinstance(data, bytes) or len(data) > MAX_SOURCE_BYTES:
                    raise ValueError('frontier_response_not_complete')
                sha = hashlib.sha256(data).hexdigest()
                publish(blobs / (sha + '.body'), data)
                with self.db() as db:
                    history = db.execute('SELECT value FROM observations WHERE source=? AND run!=? ORDER BY rowid DESC',
                                         (source['id'], request_id))
                    previous_sha = next((item.get('sha256') for item in
                        (json.loads(row['value']) for row in history) if item.get('status') == 'observed'), None)
                sampled = self.clock()
                record.update(transport, status='observed', observed_at=sampled,
                              expires_at=sampled + self.config['interval_seconds'], bytes=len(data), sha256=sha,
                              previous_sha256=previous_sha,
                              content_changed=None if previous_sha is None else sha != previous_sha,
                              evidence_ref='frontier:{}:{}:{}'.format(digest(request_id), source['id'], sha),
                              summary=summarize(data), trust='untrusted_primary_reference_not_instructions')
            except Exception as error:
                record.update(status='unavailable', error_category=safe_error(error), observed_at=self.clock())
            with self.db(write=True) as db:
                db.execute('UPDATE observations SET value=? WHERE run=? AND source=? AND attempt=?',
                           (encoded(record).decode(), request_id, source['id'], attempt))
            observed.append(record)
        local = []
        for item in self.config['local_metadata']:
            try:
                local.append(metadata_observation(item, self.clock()))
            except Exception as error:
                local.append(dict(id=item['id'], kind=item['kind'], status='unavailable', error_category=safe_error(error)))
        final = self.clock()
        evidence = model_evidence(self.config, observed, local, final)
        raw = encoded(evidence)
        evidence_sha = hashlib.sha256(raw).hexdigest()
        publish(blobs / (evidence_sha + '.json'), raw)
        complete = all(row['status'] == 'observed' for row in observed if row['required'])
        receipt = dict(SAFETY, schema=1, request_id=request_id, task_context=self.config['task_context'],
            schedule_id=self.config['schedule_id'], configuration_sha256=config_sha,
            status='completed' if complete else 'partial', observed_at=final, sources=observed,
            local_metadata=local, model_evidence_sha256=evidence_sha, model_evidence_bytes=len(raw),
            benchmark_or_route_observation_times_renewed=False,
            numeric_benchmark_imported=self.config.get('baseline_evidence_file') is not None,
            next_due_at=final + self.config['interval_seconds'] if complete else None,
            retry_at=None if complete else final + self.config['retry_seconds'],
            timer_installation_performed=False, original_native_goal_or_plan_mutated=False,
            capabilities_upgraded_or_evidence_reverified=False,
            source_scope='public GET and selected local non-secret metadata only')
        receipt['receipt_sha256'] = digest(receipt)
        receipt_raw = encoded(receipt)
        publish(blobs / (receipt['receipt_sha256'] + '.receipt'), receipt_raw)
        with self.db(write=True) as db:
            db.execute('UPDATE runs SET status=?,receipt=?,next_due=?,retry_at=? WHERE id=?',
                (receipt['status'], receipt_raw.decode(), receipt['next_due_at'], receipt['retry_at'], request_id))
        return receipt


def latest_advice(state_dir, now=None):
    """Read the last package without starting refresh, scheduling or network I/O."""
    now = time.time() if now is None else now
    root = private_dir(state_dir)
    database = root / 'frontier.sqlite'
    _checked_path(database)
    with contextlib.closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT receipt FROM runs WHERE receipt IS NOT NULL ORDER BY created DESC,rowid DESC LIMIT 1').fetchone()
    if row is None:
        raise ValueError('frontier_no_published_advice')
    receipt = json.loads(row[0], object_pairs_hook=_unique_object)
    expected = receipt.pop('receipt_sha256')
    if digest(receipt) != expected:
        raise ValueError('frontier_receipt_invalid')
    private_dir(root / 'blobs')
    evidence_sha = receipt['model_evidence_sha256']
    if not re.fullmatch(r'[0-9a-f]{64}', evidence_sha):
        raise ValueError('frontier_evidence_invalid')
    raw = _owner_bytes(root / 'blobs' / (evidence_sha + '.json'), 1024 * 1024, 'frontier_evidence_invalid')
    if len(raw) != receipt['model_evidence_bytes'] or hashlib.sha256(raw).hexdigest() != evidence_sha:
        raise ValueError('frontier_evidence_invalid')
    evidence = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object)
    sources = []
    for source in receipt['sources']:
        projected = dict(source, fresh=source['status'] == 'observed'
                         and source.get('observed_at', now + 1) <= now < source.get('expires_at', 0))
        if source['status'] == 'observed':
            sha = source['sha256']
            if not re.fullmatch(r'[0-9a-f]{64}', sha):
                raise ValueError('frontier_source_invalid')
            data = _owner_bytes(root / 'blobs' / (sha + '.body'), MAX_SOURCE_BYTES, 'frontier_source_invalid')
            if len(data) != source['bytes'] or hashlib.sha256(data).hexdigest() != sha:
                raise ValueError('frontier_source_invalid')
            projected['summary'] = summarize(data)
        sources.append(projected)
    metadata = dict(request_id=receipt['request_id'], task_context=receipt['task_context'],
        status=receipt['status'], observed_at=receipt['observed_at'], next_due_at=receipt['next_due_at'],
        retry_at=receipt['retry_at'], receipt_sha256=expected, model_evidence_sha256=evidence_sha,
        due=now >= (receipt['next_due_at'] or receipt['retry_at']),
        sources=sources, local_metadata=receipt['local_metadata'],
        source_projection='read-only projection of complete hash-checked original source bytes; receipt unchanged',
        model_calls=0, new_spend_authorized=False, native_features_restricted=False,
        timer_installation_performed=False, numeric_benchmark_imported=receipt['numeric_benchmark_imported'])
    return evidence, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description='Durable frontier refresh; public read-only observations, no inference or upgrades')
    parser.add_argument('--config', required=True)
    parser.add_argument('action', choices=('due', 'status', 'run-due', 'refresh', 'receipt'))
    parser.add_argument('--request-id')
    args = parser.parse_args(argv)
    try:
        config = json.loads(_owner_bytes(args.config, 131072, 'frontier_configuration_invalid').decode('utf8'),
                            object_pairs_hook=_unique_object)
        runtime = FrontierRefresh(config)
        if args.action in ('due', 'status'):
            output = runtime.status()
        elif args.action == 'run-due':
            output = runtime.run_due()
        else:
            if not args.request_id:
                raise ValueError('frontier_request_id_required')
            output = getattr(runtime, args.action)(args.request_id)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0 if output['status'] not in ('partial', 'pending_or_not_found') else 2
    except Exception:
        print(json.dumps(dict(SAFETY, status='frontier_unavailable_or_invalid')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
