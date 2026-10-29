"""Add S3 maintenance to an already composed incumbent, never replace its core.

Deployment code supplies the pinned helper class and private reviewer roster.
No database, account, model, receiver or credential is opened on import.
"""
import hashlib
import hmac
from inspect import getattr_static
from types import CodeType, FunctionType
from urllib.parse import urlsplit

from . import native_partial_continuation as partial
from . import operation_isolation as isolation
from .config import read_secret


GLOBAL_TABLES = ('native_partial_acceptances', 'native_partial_before_chunks',
                 'native_partial_history_parts')
REQUIRED_COVERAGE = GLOBAL_TABLES + ('tasks', 'native_sessions', 'session_chunks')


def _code_signature(code):
    # Extracted functions have different file/class/line metadata. Compare
    # executable semantics recursively, including nested comprehensions.
    constants = tuple(_code_signature(value) if isinstance(value, CodeType) else value for value in code.co_consts)
    return (code.co_code, code.co_names, code.co_varnames, code.co_argcount,
            code.co_kwonlyargcount, getattr(code, 'co_posonlyargcount', 0),
            code.co_flags, code.co_freevars, code.co_cellvars, constants)


def install_store_type(base, methods_class):
    """Install ONLY the shared quiescence helper on the original class.

    Constructors, transaction hooks, claim/update, entrance and native-scope
    wrappers stay exactly where the incumbent composed them.
    """
    if not isinstance(base, type) or not isinstance(methods_class, type):
        raise TypeError('native_partial_trusted_store_classes_required')
    method = getattr_static(methods_class, '_assert_continuation_quiescent', None)
    if not isinstance(method, FunctionType) or not callable(getattr(base, 'transaction', None)):
        raise TypeError('native_partial_pinned_quiescent_helper_required')
    if not all(callable(getattr(isolation, name, None)) for name in ('related', 'native_scope', 'native_work_unsettled')):
        raise TypeError('native_partial_incumbent_isolation_seams_required')
    installed = getattr_static(base, '_assert_continuation_quiescent', None)
    if installed is not None:
        if (not isinstance(installed, FunctionType)
                or _code_signature(installed.__code__) != _code_signature(method.__code__)):
            raise TypeError('native_partial_existing_quiescence_helper_changed')
        return base
    # Use the original Store's exception class, as the old HTTP dispatcher may
    # catch exactly that class. Preserve other trusted function globals.
    conflict = None
    for owner in reversed(base.__mro__):
        function = vars(owner).get('update_task')
        if isinstance(function, FunctionType):
            candidate = function.__globals__.get('Conflict')
            if isinstance(candidate, type) and issubclass(candidate, Exception):
                conflict = candidate
                break
    if conflict is None:
        raise TypeError('native_partial_incumbent_conflict_required')
    namespace = dict(method.__globals__, Conflict=conflict, isolation=isolation)
    installed = FunctionType(method.__code__, namespace, method.__name__, method.__defaults__, method.__closure__)
    installed.__kwdefaults__ = method.__kwdefaults__
    base._assert_continuation_quiescent = installed
    return base


def install_schema(store, methods_class):
    install_store_type(type(store), methods_class)
    with store.transaction() as db:
        partial.tables(db)
    return {'installed_tables': list(GLOBAL_TABLES), 'coverage_refreshed': False,
            'tasks_reconciled': False, 'native_turn_started': False}


def _roster(api, config):
    if not isinstance(config, dict) or set(config) != {'reviewers'}:
        raise ValueError('native_partial_private_reviewer_config_required')
    rows = config['reviewers']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError('native_partial_explicit_reviewers_required')
    peers = list(api.peers)
    identities, roster, new_peers = set(), [], []
    for item in rows:
        if not isinstance(item, dict) or set(item) != {'reviewer_id', 'token_file'}:
            raise ValueError('native_partial_reviewer_binding_invalid')
        peer = {'role': 'native_maintenance', 'reviewer_id': item['reviewer_id']}
        partial.maintainer(peer)
        if item['reviewer_id'] in identities:
            raise ValueError('native_partial_duplicate_reviewer')
        identities.add(item['reviewer_id'])
        secret = read_secret(item['token_file'])
        existing = False
        for other_secret, other in peers + new_peers:
            if hmac.compare_digest(secret, other_secret):
                if other.get('role') != 'native_maintenance' or other.get('reviewer_id') != item['reviewer_id']:
                    raise ValueError('ambiguous_native_maintenance_credential')
                existing = True
        if not existing:
            new_peers.append((secret, peer))
        roster.append((item['reviewer_id'], hashlib.sha256(secret.encode()).hexdigest()))
    # Also reject collisions inherited from an old API which had no new-role
    # constructor validation. Never resolve ambiguity by peer list ordering.
    combined = peers + new_peers
    for index, (secret, peer) in enumerate(combined):
        for other_secret, other in combined[:index]:
            if ('native_maintenance' in (peer.get('role'), other.get('role'))
                    and hmac.compare_digest(secret, other_secret)
                    and (peer.get('role'), peer.get('reviewer_id')) != (other.get('role'), other.get('reviewer_id'))):
                raise ValueError('ambiguous_native_maintenance_credential')
    return roster, new_peers


def require_coverage(api):
    journal = getattr(api, '_business_journal', None)
    if journal is None or not callable(getattr(journal, 'status', None)):
        raise ValueError('native_partial_business_coverage_refresh_required')
    status = journal.status()
    covered = status.get('manifest', {}).get('tables', {})
    if (status.get('coverage_issues') or status.get('ledger_coverage') != 'declared_tables_current'
            or not isinstance(covered, dict) or not set(REQUIRED_COVERAGE) <= set(covered)):
        raise ValueError('native_partial_business_coverage_refresh_required')


def attach(api, config, methods_class):
    """Called after the complete original ready callback; wraps dispatch once.

    Schema creation is explicit, but never silently refreshes journal coverage.
    The previous callback and original peer/dispatch behavior remain installed.
    """
    roster, new_peers = _roster(api, config)
    installed = getattr(api, '_native_partial_maintenance', None)
    if installed is not None:
        if installed['roster'] != roster:
            raise ValueError('native_partial_live_reviewer_change_requires_restart')
        return installed['installation']
    installation = install_schema(api.store, methods_class)
    previous = api.dispatch

    def dispatch(method, path, payload, peer):
        parsed = urlsplit(path)
        if peer.get('role') == 'native_maintenance' or parsed.path.startswith(partial.PREFIX):
            partial.maintainer(peer)
            if method != 'POST' or parsed.query or parsed.fragment:
                raise PermissionError('native_partial_route_not_authorized')
            if parsed.path in (partial.PREFIX + 'history', partial.PREFIX + 'accept'):
                require_coverage(api)
            result = partial.dispatch(api, method, path, payload, peer)
            if result is NotImplemented:
                raise PermissionError('native_partial_route_not_authorized')
            return result
        return previous(method, path, payload, peer)

    api.peers.extend(new_peers)
    api.dispatch = dispatch
    api._native_partial_maintenance = {'roster': roster, 'previous_dispatch': previous,
                                      'installation': installation}
    return installation


def receipt_resolver(client):
    """Deployment-owned Client with a dedicated maintenance credential only.

    Caller JSON supplies only an acceptance id. The receipt body comes from
    the authenticated original authority, never from a model's report.
    """
    if not callable(getattr(client, 'request', None)):
        raise TypeError('native_partial_trusted_maintenance_client_required')

    def resolve(acceptance_id):
        partial.name(acceptance_id)
        value = client.request(partial.PREFIX + 'receipt', {'acceptance_id': acceptance_id})
        if (not isinstance(value, dict) or value.get('protocol') != partial.PROTOCOL
                or value.get('acceptance_id') != acceptance_id or value.get('accepted') is not True):
            raise ValueError('native_partial_original_acceptance_not_confirmed')
        return value
    return resolve
