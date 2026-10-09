"""Task-bound local evidence snapshot, not a scoring or execution controller.

All sections come from one local authority transaction. Remote announcements
remain declared estimates; observations keep units, scope and workload. Reading
never probes networks, authorizes a grant, admits work or replays an operation.
"""
import hashlib
import json

from .resources import _finite, _name, _safe_metadata
from .store import Conflict


MAX_CONTEXT_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_RECORDS = 100


class RoutingContext:
    def __init__(self, store, authority, registry, allocations=None, projection=None):
        self.store, self.authority, self.registry = store, _name(authority, 'routing_authority'), registry
        self.allocations, self.projection = allocations, projection
        if any(component is not None and component.store.path != store.path
               for component in (registry, allocations, projection)):
            raise ValueError('routing_context_requires_same_authority_ledger')

    @staticmethod
    def _section(rows, limit, status='observed'):
        return {'status': status, 'records': rows[:limit], 'truncated': len(rows) > limit,
                'returned': min(len(rows), limit), 'complete_for_filter': len(rows) <= limit}

    def read(self, task_id, node, epoch, kind=None, issuer=None, include_unavailable=True,
             limit=20, observation_max_age_seconds=None):
        for value, name in ((task_id, 'routing_task'), (node, 'routing_node')):
            _name(value, name)
        if type(epoch) is not int or epoch < 1:
            raise ValueError('invalid_routing_task_epoch')
        if kind is not None:
            _name(kind, 'routing_kind')
        if issuer is not None:
            _name(issuer, 'routing_issuer')
        if type(include_unavailable) is not bool:
            raise ValueError('invalid_routing_include_unavailable')
        if type(limit) is not int or not 1 <= limit <= MAX_CONTEXT_RECORDS:
            raise ValueError('invalid_routing_context_limit')
        if observation_max_age_seconds is not None:
            observation_max_age_seconds = _finite(observation_max_age_seconds, 'routing_observation_age')
            if not 0 < observation_max_age_seconds <= 86400:
                raise ValueError('invalid_routing_observation_age')
        with self.store.transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not self.store._task_is_live(db, task, node, epoch):
                raise Conflict('stale_task_lease')
            now = self.store.clock()
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            where, args = [], []
            if kind is not None:
                where.append('kind=?')
                args.append(kind)
            if not include_unavailable:
                where.extend(('revoked=0', 'deadline>?', "health NOT IN ('failed','unavailable')"))
                args.append(now)
            query = ('SELECT id,principal,kind,description,spec,epoch,health,deadline,revoked,created,updated,redacted '
                     'FROM capabilities') + (' WHERE ' + ' AND '.join(where) if where else '')
            caps = db.execute(query + ' ORDER BY updated DESC,id LIMIT ?', args + [limit + 1]).fetchall()
            cap_views = []
            for row in caps:
                view = self.registry._view(row)
                view.update(available=self.registry._live(row, now), lease_expired=row['deadline'] <= now)
                cap_views.append(view)
            sections = {'local_capabilities': self._section(cap_views, limit)}
            selected = {row['id']: row for row in caps[:limit]}
            ids = list(selected)
            placeholders = ','.join('?' for identity in ids)
            metrics, edges, bindings = [], [], []
            if ids:
                for row in db.execute('SELECT id,fingerprint,capability_id,capability_epoch,metric,value,unit,source,actor,'
                                      'sample_time,received,evidence,verification,redacted FROM capability_metrics WHERE capability_id IN (' + placeholders +
                                      ') ORDER BY received DESC,id LIMIT ?', ids + [limit + 1]):
                    cap = selected[row['capability_id']]
                    metric = self.registry._metric_view(row, cap['epoch'])
                    age = max(0, now - row['sample_time'])
                    metric.update(sample_age_seconds=age, sample_time_in_future=row['sample_time'] > now,
                                  received_age_seconds=max(0, now - row['received']),
                                  requested_max_age_seconds=observation_max_age_seconds,
                                  trusted_verifier_current=row['actor'] in self.registry.trusted_verifiers,
                                  independent_of_current_provider=row['actor'] != cap['principal'],
                                  admission_policy_rechecked=False,
                                  freshness_deadline=(None if observation_max_age_seconds is None else
                                      min(cap['deadline'], row['sample_time'] + observation_max_age_seconds)),
                                  scope_workload_checked=False,
                                  fresh_for_requested_age=(None if observation_max_age_seconds is None else
                                      bool(metric['current_epoch'] and row['sample_time'] <= now
                                           and age < observation_max_age_seconds
                                           and self.registry._live(cap, now))))
                    metrics.append(metric)
                for row in db.execute('SELECT id,actor,source,target,source_epoch,target_epoch,relation,spec,redacted,deadline,created '
                                      'FROM capability_edges WHERE source IN (' + placeholders +
                                      ') AND target IN (' + placeholders + ') ORDER BY id LIMIT ?', ids + ids + [limit + 1]):
                    edge = dict(row)
                    edge['spec'] = json.loads(edge['spec'])
                    edge['redacted'] = bool(edge['redacted'])
                    edge['available'] = bool(row['deadline'] > now and
                        selected[row['source']]['epoch'] == row['source_epoch'] and
                        selected[row['target']]['epoch'] == row['target_epoch'] and
                        self.registry._live(selected[row['source']], now) and
                        self.registry._live(selected[row['target']], now))
                    edges.append(edge)
                if 'capability_pool_bindings' in tables:
                    bindings = [dict(row) for row in db.execute('SELECT capability_id,capability_epoch,pool_id,pool_epoch,dimension,'
                        'quantity,unit,usage_key,epoch,new_spend_minor FROM capability_pool_bindings WHERE capability_id IN (' +
                        placeholders + ') ORDER BY capability_id,pool_id,dimension LIMIT ?', ids + [limit + 1])]
            sections['local_observations'] = self._section(metrics, limit)
            sections['local_edges'] = self._section(edges, limit)
            sections['local_pool_bindings'] = self._section(bindings, limit,
                'observed' if 'capability_pool_bindings' in tables else 'not_initialized')
            for name in ('local_observations', 'local_edges', 'local_pool_bindings'):
                sections[name]['selection_scope'] = 'selected_local_capability_ids_before_byte_truncation'
            pools = []
            if self.allocations is not None:
                for row in db.execute('SELECT id,dimensions,epoch,deadline,revoked,created,updated '
                                      'FROM resource_pools ORDER BY id LIMIT ?', (limit + 1,)):
                    view = self.allocations._pool_view(db, row)
                    view['available'] = bool(not row['revoked'] and row['deadline'] > now)
                    pools.append(view)
            sections['local_pools'] = self._section(pools, limit,
                'observed' if self.allocations is not None else 'not_initialized')
            remote = []
            if self.projection is not None:
                remote = self.projection._discover_view(db, now, issuer, kind, include_unavailable, limit + 1)['capabilities']
            sections['federated_capabilities'] = self._section(remote, limit,
                'observed' if self.projection is not None else 'disabled')
            executions = []
            if 'remote_delegations' in tables:
                for row in db.execute('SELECT child_id,parent_epoch,caller_node,peer,message_id,remote_task_id,remote_status,terminal,observed '
                                      'FROM remote_delegations WHERE parent_id=? ORDER BY child_id LIMIT ?', (task_id, limit + 1)):
                    executions.append(dict(row, kind='remote_delegation', reference_id=row['child_id'],
                        decision_binding_verified=False, execution_verified=False))
            sections['remote_execution_candidates'] = self._section(executions, limit,
                'observed' if 'remote_delegations' in tables else 'not_initialized')
            executions = []
            if 'managed_allocations' in tables:
                for row in db.execute('SELECT operation_id,actor,task_epoch,task_node,leader_epoch,state,epoch,deadline '
                                      'FROM managed_allocations WHERE task_id=? ORDER BY operation_id LIMIT ?', (task_id, limit + 1)):
                    executions.append(dict(row, kind='managed_allocation', reference_id=row['operation_id'],
                        decision_binding_verified=False, execution_verified=False))
            # Independent bounds prevent one effect family from starving the other.
            sections['managed_execution_candidates'] = self._section(executions, limit,
                'observed' if 'managed_allocations' in tables else 'not_initialized')
            data = {'format': 'mesh-routing-context/1', 'authority': self.authority,
                    'task': {'id': task_id, 'node': node, 'epoch': epoch, 'leader_epoch': task['leader_epoch']},
                    'as_of': now, 'database_atomic_snapshot': True, 'sections': sections,
                    'selected_local_capability_ids': ids,
                    'model_ranking_applied': False, 'network_probed': False,
                    'grant_checked': False, 'managed_invocation_authorized': False,
                    'native_tools_intercepted': False, 'global_consensus_verified': False,
                    'remote_metrics_included': False, 'remote_pools_included': False,
                    'observation_freshness_policy': ('not_applied' if observation_max_age_seconds is None else 'caller_requested_age_only'),
                    'independent_failure_domains_verified': False}
            data, redacted = _safe_metadata(data)
            data['secret_fields_redacted'] = redacted
            # Bounded partial views are explicit; never silently call them complete.
            data['byte_budget_truncated'] = False
            # Include the final checksum field in the wire budget before hashing.
            data['evidence_sha256'] = '0' * 64
            while len(json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()) > MAX_CONTEXT_BYTES:
                candidates = [(name, section) for name, section in data['sections'].items() if section['records']]
                if not candidates:
                    raise ValueError('routing_context_too_large')
                name, section = max(candidates, key=lambda entry: len(json.dumps(entry[1]['records'], ensure_ascii=False).encode()))
                section['records'].pop()
                section.update(truncated=True, complete_for_filter=False, returned=len(section['records']))
                data['byte_budget_truncated'] = True
            data.pop('evidence_sha256')
            encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
            data['evidence_sha256'] = hashlib.sha256(encoded).hexdigest()
            return data
