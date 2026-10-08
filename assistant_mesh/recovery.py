"""Laptop-only native refresh. The cloud remains the ONLY iLink poller."""
import importlib
import json
import sys
import time

from .config import private_json
from .worker import Client


def run(config):
    client = Client(config)
    # Load the installed capability, not an ad-hoc GUI sender or a second poller.
    sys.path.insert(0, config['skill_scripts'])
    native = importlib.import_module('native_cli')
    account = private_json(config['native_account'])
    settings = account.get('native_recovery', {})
    if (not settings.get('enabled') or settings.get('ilink_user_id') != account.get('ilink_user_id')
            or settings.get('ilink_bot_id') != account.get('ilink_bot_id')):
        raise ValueError('native_refresh_not_bound_to_current_owner')
    while True:
        try:
            value = client.request('/v1/recovery/claim', {})['renewal']
            if value:
                # Unknown native send result is NEVER replayed under a new ID.
                native.send_text(value['marker'], value['request_id'], settings['native_chat'], timeout=120)
        except (OSError, ValueError):
            pass
        time.sleep(5)
