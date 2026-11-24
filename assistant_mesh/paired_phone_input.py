"""One locally approved HID action for an explicitly bonded nearby phone."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import stat
import struct
import sys
from contextlib import contextmanager
from pathlib import Path

from .config import private_json
from .ingress import _identifier
from .phone_computer_use import canonical, private_output


BINDING = 'amita-paired-phone-input-binding/1'
CONTRACT = 'amita-paired-phone-input/1'
SESSION_SECONDS = 30
NEUTRAL = {'keyboard': bytes(8), 'pointer': bytes(4)}
KEYBOARD_ID, POINTER_ID = 1, 2

# HID report IDs are in the Report Reference descriptors. GATT report values
# carry only the content of the reports, without a second copy of the ID.
KEYBOARD_MAP = bytes.fromhex('''
    05 01 09 06 a1 01 85 01
    05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02
    75 08 95 01 81 01
    05 08 19 01 29 05 75 01 95 05 91 02
    75 03 95 01 91 01
    05 07 19 00 29 65 15 00 25 65 75 08 95 06 81 00 c0
''')
POINTER_MAP = bytes.fromhex('''
    05 01 09 02 a1 01 85 02 09 01 a1 00
    05 09 19 01 29 03 15 00 25 01 75 01 95 03 81 02
    75 05 95 01 81 01
    05 01 09 30 09 31 09 38 15 81 25 7f 75 08 95 03 81 06 c0 c0
''')
KEYS = {char: code for code, char in enumerate('abcdefghijklmnopqrstuvwxyz1234567890', 4)}
KEYS.update(enter=40, escape=41, backspace=42, tab=43, space=44, home=74,
            page_up=75, delete=76, end=77, page_down=78, right=79, left=80, down=81, up=82)
MODIFIERS = dict(ctrl=1, shift=2, alt=4, meta=8)
PUNCTUATION = dict(zip("-=[]\\;'`,./", (45, 46, 47, 48, 49, 51, 52, 53, 54, 55, 56)))
SHIFTED = dict(zip('!@#$%^&*()_+{}|:"~<>?', '1234567890-=[]\\;\'`,./'))
BUTTONS = dict(left=1, right=2, middle=4)


def encode_reports(action):
    if not isinstance(action, dict) or type(action.get('kind')) is not str:
        raise ValueError('invalid_paired_phone_action')
    kind = action['kind']
    if kind == 'key' and set(action) == {'kind', 'key', 'modifiers'}:
        key, modifiers = action['key'], action['modifiers']
        if (type(key) is not str or key not in KEYS or not isinstance(modifiers, list)
                or any(type(mod) is not str or mod not in MODIFIERS for mod in modifiers)
                or len(set(modifiers)) != len(modifiers)):
            raise ValueError('invalid_paired_phone_key')
        pressed = bytes((sum(MODIFIERS[mod] for mod in modifiers), 0, KEYS[key])) + bytes(5)
        return 'keyboard', (pressed, NEUTRAL['keyboard'])
    if kind == 'text' and set(action) == {'kind', 'text'}:
        text = action['text']
        if type(text) is not str or not 1 <= len(text) <= 32:
            raise ValueError('invalid_paired_phone_text')
        reports = []
        for char in text:
            if 'A' <= char <= 'Z':
                usage, modifier = KEYS[char.lower()], 2
            elif char in SHIFTED:
                base = SHIFTED[char]
                usage, modifier = KEYS.get(base, PUNCTUATION.get(base)), 2
            else:
                usage = KEYS.get(char, PUNCTUATION.get(char)) if char != ' ' else KEYS['space']
                modifier = 0
            if usage is None:
                raise ValueError('paired_phone_printable_us_ascii_required')
            reports.extend((bytes((modifier, 0, usage)) + bytes(5), NEUTRAL['keyboard']))
        return 'keyboard', tuple(reports)
    if kind == 'move' and set(action) == {'kind', 'dx', 'dy', 'wheel'}:
        axes = action['dx'], action['dy'], action['wheel']
        if not any(axes) or any(type(value) is not int or not -127 <= value <= 127 for value in axes):
            raise ValueError('invalid_paired_phone_pointer_delta')
        return 'pointer', (struct.pack('Bbbb', 0, *axes),)
    if kind == 'click' and set(action) == {'kind', 'button'}:
        button = action['button']
        if type(button) is not str or button not in BUTTONS:
            raise ValueError('invalid_paired_phone_button')
        return 'pointer', (bytes((BUTTONS[button], 0, 0, 0)), NEUTRAL['pointer'])
    raise ValueError('invalid_paired_phone_action')


def load_binding(path):
    binding = private_json(path)
    fields = {'format', 'phone_node_id', 'adapter', 'peer_address', 'report_modes', 'state_dir'}
    if not isinstance(binding, dict) or set(binding) != fields or binding['format'] != BINDING:
        raise ValueError('invalid_paired_phone_binding')
    _identifier(binding['phone_node_id'], 'paired_phone_node_id', 160)
    adapter, address, modes, state = (binding[name] for name in ('adapter', 'peer_address', 'report_modes', 'state_dir'))
    if (not isinstance(adapter, str) or not re.fullmatch(r'hci[0-9]+', adapter)
            or not isinstance(address, str) or not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', address)
            or not isinstance(modes, list) or not modes
            or any(type(mode) is not str or mode not in NEUTRAL for mode in modes)
            or len(set(modes)) != len(modes)
            or not isinstance(state, str) or not Path(state).is_absolute()):
        raise ValueError('invalid_paired_phone_binding')
    return binding


def prepare(binding, request):
    fields = {'format', 'task_id', 'task_epoch', 'operation_id', 'phone_node_id', 'action'}
    if (not isinstance(request, dict) or set(request) != fields or request['format'] != CONTRACT
            or request['phone_node_id'] != binding['phone_node_id']
            or type(request['task_epoch']) is not int or not 1 <= request['task_epoch'] <= 2**63 - 1):
        raise ValueError('invalid_paired_phone_scope')
    _identifier(request['task_id'], 'paired_phone_task_id', 180)
    _identifier(request['operation_id'], 'paired_phone_operation_id', 180)
    mode, reports = encode_reports(request['action'])
    if mode not in binding['report_modes']:
        raise PermissionError('paired_phone_report_mode_disabled')
    target = {'adapter': binding['adapter'], 'peer_address': binding['peer_address']}
    digest = hashlib.sha256(canonical({'request': request, 'target': target}).encode()).hexdigest()
    return mode, reports, digest


def validate_peer(binding, properties, *, connected):
    if (properties.get('Adapter') != '/org/bluez/' + binding['adapter']
            or properties.get('Address') != binding['peer_address']
            or properties.get('Paired') != True or properties.get('Bonded') != True):
        raise PermissionError('paired_phone_exact_bond_required')
    if connected and properties.get('Connected') != True:
        raise PermissionError('paired_phone_connection_required')


def validate_att(sender, owner, options, peer_path):
    if sender != owner or options.get('device') != peer_path or options.get('link') != 'LE':
        raise PermissionError('paired_phone_exact_le_att_peer_required')


@contextmanager
def state_lock(binding):
    state = Path(binding['state_dir'])
    descriptor = os.open(state, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError('paired_phone_private_state_required')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PermissionError('paired_phone_local_session_already_active') from None
        yield state, descriptor
    finally:
        os.close(descriptor)


class Receipt:
    def __init__(self, state, directory_fd, operation_id, digest):
        self.directory_fd = directory_fd
        self.base = state / hashlib.sha256(operation_id.encode()).hexdigest()
        self.identity = {'format': CONTRACT, 'operation_id': operation_id, 'request_sha256': digest,
                         'phone_effect_verified': False}

    def read(self):
        for suffix in ('.result', '.claim'):
            try:
                value = private_json(self.base.with_suffix(suffix))
            except ValueError:
                path = self.base.with_suffix(suffix)
                if path.exists() or path.is_symlink():
                    raise
                continue
            if (not isinstance(value, dict) or not isinstance(value.get('state'), str)
                    or any(value.get(key) != expected for key, expected in self.identity.items())):
                raise ValueError('paired_phone_original_operation_conflict')
            return value
        return dict(self.identity, state='not_started')

    def write(self, suffix, state, **details):
        value = dict(self.identity, state=state, **details)
        output = private_output(self.base.with_suffix(suffix), canonical(value).encode())
        os.fsync(self.directory_fd)
        if output['reused']:
            raise ValueError('paired_phone_original_operation_already_recorded')
        return value


def confirm_session(tty, binding, request, digest, advertise):
    phase = 'finite HID service advertisement' if advertise else 'connected-only HID service'
    tty.write(('Open a %s for up to %d seconds for %s.\n' % (
        phase, SESSION_SECONDS, binding['peer_address'])).encode())
    tty.write((canonical(request) + '\n').encode())
    challenge = 'OPEN ' + digest[:12]
    tty.write(('Type %s to continue. ' % challenge).encode())
    if (not select.select([tty], [], [], 30)[0] or tty.read(128) != (challenge + '\n').encode()
            or os.tcgetpgrp(tty.fileno()) != os.getpgrp()):
        raise PermissionError('paired_phone_terminal_confirmation_required')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True)
    parser.add_argument('--request', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare', help='validate the owner binding and exact action')
    commands.add_parser('status', help='read the original local operation receipt')
    run = commands.add_parser('run', help='ask the local owner to approve one action')
    run.add_argument('--advertise', action='store_true', help='offer a finite non-discoverable HID service')
    args = parser.parse_args(argv)
    try:
        binding, request = load_binding(args.binding), private_json(args.request)
        mode, reports, digest = prepare(binding, request)
        if args.command == 'prepare':
            result = {'format': CONTRACT, 'operation_id': request['operation_id'], 'request_sha256': digest,
                      'report_mode': mode, 'report_count': len(reports), 'state': 'prepared'}
        else:
            with state_lock(binding) as (state, descriptor):
                receipt = Receipt(state, descriptor, request['operation_id'], digest)
                result = receipt.read()
                if args.command == 'run' and result['state'] == 'not_started':
                    from .bluez_phone_hid import run_session

                    with open('/dev/tty', 'r+b', buffering=0) as tty:
                        if os.tcgetpgrp(tty.fileno()) != os.getpgrp():
                            raise PermissionError('paired_phone_foreground_terminal_required')
                        confirm_session(tty, binding, request, digest, args.advertise)
                        result = run_session(binding, mode, reports, receipt, tty, args.advertise)
        print(canonical(result))
        return int(args.command == 'run' and result['state'] != 'reports_queued_to_bluez')
    except (ImportError, OSError, ValueError) as error:
        print(canonical({'format': CONTRACT, 'error': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
