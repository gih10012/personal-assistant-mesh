"""Admission for a staged OAuth server with the original typed Raft grant."""
import os

from .business_journal import digest
from .business_writer_grant import confirm_grant
from .config import private_json
from .oauth_state_bundle import (FORMAT, _publish_record, _read, binding, decode, inspect_staging,
                                 key_references, safe_path, sha)
from .oauth_state_boundary import STAGING_MARKER
from .oauth_writer_handoff import assemble_transfer, target_directory_sha256


FORMAT_ACTIVATION = 'amita-oauth-writer-activation/1'
RECORD_NAME = 'writer-activation.json'
CONFIG_FIELDS = {'plan', 'source', 'target', 'source_public_key', 'target_public_key', 'voter_config'}


class OAuthWriterActivation:
    def __init__(self, document, config, *, allow_loopback_for_tests=False):
        if (not isinstance(config, dict) or set(config) != CONFIG_FIELDS
                or not all(isinstance(path, str) and path for path in config.values())):
            raise ValueError('oauth_writer_private_activation_configuration_required')
        self.document, bound = binding(document, allow_loopback_for_tests)
        self.allow_loopback = allow_loopback_for_tests
        self.root = safe_path(self.document['state']['directory'])
        source, target = (private_json(safe_path(config[role])) for role in ('source', 'target'))
        self.proposal = assemble_transfer(private_json(safe_path(config['plan'])), source, target,
                                          config['source_public_key'], config['target_public_key'])
        checkpoint = self.proposal['checkpoint']
        if digest(bound) != checkpoint['binding_sha256']:
            raise ValueError('oauth_writer_activation_policy_changed')
        self.directory_sha256 = target['body']['evidence']['target_directory_sha256']
        self.voter_config = safe_path(config['voter_config'])

    def _local_record(self, original_keys):
        root = self.root
        checkpoint = self.proposal['checkpoint']
        marker = _read(root / STAGING_MARKER, 32768)
        if decode(marker) != dict(format=FORMAT, bundle_sha256=checkpoint['bundle_sha256'], activation_authorized=False):
            raise ValueError('oauth_writer_stage_marker_conflict')
        self._check_local(original_keys)
        pinned = dict(format=FORMAT_ACTIVATION, operation_id=self.proposal['operation_id'],
            proposal_sha256=digest(self.proposal), target_directory_sha256=self.directory_sha256,
            source_stage_marker_sha256=sha(marker), keys_sha256=digest(original_keys))
        record_path = root.parent / RECORD_NAME
        record = None
        if os.path.lexists(record_path):
            record = decode(_read(record_path, 32768))
            if (not isinstance(record, dict) or set(record) != set(pinned) | {'first_confirmation', 'sha256'}
                    or any(record[key] != value for key, value in pinned.items())
                    or not isinstance(record['first_confirmation'], dict)
                    or record['sha256'] != digest({key: value for key, value in record.items() if key != 'sha256'})):
                raise ValueError('oauth_writer_activation_record_conflict')
        return record_path, record, pinned, marker

    def confirm_serving_grant(self):
        """Check an already activated local target without starting it."""
        original_keys = key_references(self.document)
        _, record, _, _ = self._local_record(original_keys)
        if record is None:
            raise ValueError('oauth_writer_activation_record_required')
        current = confirm_grant(self.proposal, private_json(self.voter_config))
        self._check_local(original_keys)
        if record['first_confirmation'].get('receipt') != current['receipt']:
            raise ValueError('oauth_writer_activation_original_grant_conflict')
        return current

    def admit(self, original_keys):
        """Run under the gateway's directory lock, before any SDK or HTTP work."""
        root = self.root
        checkpoint = self.proposal['checkpoint']
        record_path, record, pinned, marker = self._local_record(original_keys)
        if record is None:
            checked = inspect_staging(root.parent, self.document, checkpoint['bundle_sha256'],
                                      allow_loopback_for_tests=self.allow_loopback)
            if checked != checkpoint['target_state_sha256']:
                raise ValueError('oauth_writer_signed_staging_changed')
        current = confirm_grant(self.proposal, private_json(self.voter_config))
        self._check_local(original_keys)
        if _read(root / STAGING_MARKER, 32768) != marker:
            raise ValueError('oauth_writer_stage_marker_conflict')
        if record is None:
            # Tie the directory to the original activation. Leaving the staging
            # marker in place keeps plain and older gateway factories closed.
            record = dict(pinned, first_confirmation=current)
            record['sha256'] = digest(record)
            _publish_record(record_path, record)
        elif record['first_confirmation'].get('receipt') != current['receipt']:
            raise ValueError('oauth_writer_activation_original_grant_conflict')
        return pinned['source_stage_marker_sha256']

    def _check_local(self, original_keys):
        if target_directory_sha256(self.root) != self.directory_sha256:
            raise ValueError('oauth_writer_target_directory_changed')
        if key_references(self.document) != original_keys:
            raise ValueError('oauth_writer_activation_keys_changed')
