"""Owner-side receipts for the current published-environment Cloud UI.

This is a Mesh evidence envelope, NOT an OpenAI API/schema. Official product
documentation describes the current UI workflow, but does not specify its URL
or receipt schema. A trusted normal-UI verifier must establish that workflow;
URL syntax and hashes alone never establish Cloud execution or task identity.
No browser, account, network, credential or task operation happens here.
"""
import json
import math
import re
from urllib.parse import urlsplit


PROTOCOL = 'mesh-cloud-current-ui-receipt/1'
RECEIPT_SOURCE = 'owner_current_ui_receipt'
STATUSES = frozenset(('pending', 'running', 'completed', 'error'))
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_FIELDS = frozenset(('format', 'job_id', 'intent_sha256', 'owner_binding_sha256',
                     'mesh_task_id', 'mesh_epoch', 'request_id', 'environment_id',
                     'provider_task_id', 'provider_url', 'workflow',
                     'execution_location', 'launch', 'reopened'))


def _observation(value):
    if (not isinstance(value, dict) or set(value) != {'evidence_sha256', 'observed_at'}
            or not isinstance(value['evidence_sha256'], str)
            or not _HASH.fullmatch(value['evidence_sha256'])
            or type(value['observed_at']) not in (int, float)
            or not math.isfinite(value['observed_at'])
            or not 0 <= value['observed_at'] < (1 << 53)):
        raise ValueError('invalid_current_cloud_ui_observation')
    return value


def _task_url(url, provider_id):
    """Canonical same-origin reference only, never a Cloud identity proof.

    Deliberately no guessed /local or /c route semantics. The verifier must
    inspect the real saved task and establish the exact URL/ID association.
    """
    try:
        parsed = urlsplit(url)
        parts = parsed.path.split('/')[1:]
        if (not isinstance(url, str) or not 1 <= len(url) <= 2048
                or not all(33 <= ord(char) < 127 for char in url)
                or parsed.scheme != 'https' or parsed.netloc != 'chatgpt.com'
                or parsed.query or parsed.fragment or any(char in url for char in '? #%\\')
                or not parts or parts[-1] != provider_id
                or any(not _ID.fullmatch(part) for part in parts)
                or any(part.lower() in ('settings', 'setup', 'environments') for part in parts)
                or any(part.lower().startswith(('client-new-thread', 'client_new_thread')) for part in parts)
                or url != 'https://chatgpt.com/' + '/'.join(parts)):
            raise ValueError('unsafe')
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid_current_cloud_ui_task_url') from None


def validate_receipt(receipt, binding, *, created_at):
    """Validate immutable local correlation; caller separately verifies the UI.

    binding is computed from the original private journal, never supplied by
    the model or the Cloud VM. launch and reopened reference two retained normal
    UI observations, not title matching, hidden APIs or a setup conversation.
    """
    if (not isinstance(receipt, dict) or set(receipt) != _FIELDS
            or receipt['format'] != PROTOCOL
            or receipt['workflow'] != 'published_environment_task'
            or receipt['execution_location'] != 'openai_cloud'):
        raise ValueError('invalid_current_cloud_ui_receipt')
    for field in ('job_id', 'intent_sha256', 'owner_binding_sha256', 'mesh_task_id',
                  'mesh_epoch', 'request_id', 'environment_id'):
        if type(receipt[field]) is not type(binding[field]) or receipt[field] != binding[field]:
            raise ValueError('current_cloud_ui_intent_binding_mismatch')
    provider_id = receipt['provider_task_id']
    if (not isinstance(provider_id, str) or not _ID.fullmatch(provider_id)
            or provider_id.lower().startswith(('client-new-thread', 'client_new_thread', 'setup'))):
        raise ValueError('invalid_current_cloud_ui_task_id')
    _task_url(receipt['provider_url'], provider_id)
    launch, reopened = _observation(receipt['launch']), _observation(receipt['reopened'])
    if (launch['observed_at'] < created_at
            or reopened['observed_at'] <= launch['observed_at']
            or launch['evidence_sha256'] == reopened['evidence_sha256']):
        raise ValueError('current_cloud_ui_saved_task_evidence_required')
    try:
        raw = json.dumps(receipt, ensure_ascii=False, allow_nan=False).encode('utf8')
        if len(raw) > 16384:
            raise ValueError('large')
        return json.loads(raw.decode('utf8'))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('invalid_current_cloud_ui_receipt') from None
