import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from assistant_mesh import paired_phone_input as hid


class PairedPhoneInputTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.binding = {
            'format': hid.BINDING, 'phone_node_id': 'test-phone', 'adapter': 'hci7',
            'peer_address': '00:11:22:33:44:55', 'report_modes': ['pointer', 'keyboard'],
            'state_dir': str(self.root),
        }
        self.request = {
            'format': hid.CONTRACT, 'task_id': 'task:phone', 'task_epoch': 4,
            'operation_id': 'op:original', 'phone_node_id': 'test-phone',
            'action': {'kind': 'key', 'key': 'tab', 'modifiers': ['shift']},
        }

    def test_keyboard_reports_cover_press_and_release(self):
        mode, reports = hid.encode_reports(self.request['action'])
        self.assertEqual(mode, 'keyboard')
        self.assertEqual(reports, (b'\x02\x00\x2b' + bytes(5), bytes(8)))
        mode, reports = hid.encode_reports({'kind': 'text', 'text': 'Aa! ?'})
        self.assertEqual(mode, 'keyboard')
        self.assertEqual([report[:3].hex() for report in reports[::2]],
                         ['020004', '000004', '02001e', '00002c', '020038'])
        self.assertTrue(all(report == bytes(8) for report in reports[1::2]))
        with self.assertRaises(ValueError):
            hid.encode_reports({'kind': 'text', 'text': '中文'})

    def test_pointer_report_boundaries(self):
        mode, reports = hid.encode_reports({'kind': 'move', 'dx': -127, 'dy': 127, 'wheel': -1})
        self.assertEqual((mode, reports), ('pointer', (b'\x00\x81\x7f\xff',)))
        mode, reports = hid.encode_reports({'kind': 'click', 'button': 'right'})
        self.assertEqual((mode, reports), ('pointer', (b'\x02\x00\x00\x00', bytes(4))))
        with self.assertRaises(ValueError):
            hid.encode_reports({'kind': 'move', 'dx': True, 'dy': 1, 'wheel': 0})

    def test_relative_gesture_releases_between_actions(self):
        drag = {'kind': 'drag', 'button': 'right', 'deltas': [{'dx': 12, 'dy': -5}, {'dx': 1, 'dy': 0}]}
        hold = {'kind': 'long_press', 'button': 'left', 'hold_ms': 700}
        mode, reports = hid.encode_plan({'kind': 'gesture', 'steps': [drag, hold]})
        self.assertEqual(mode, 'pointer')
        self.assertEqual([step.payload.hex() for step in reports],
                         ['02000000', '020cfb00', '02010000', '00000000', '01000000', '00000000'])
        self.assertEqual([step.delay_ms for step in reports], [20] * 5 + [700])
        invalid = (
            {'kind': 'gesture', 'steps': [drag, {'kind': 'tap', 'x': 1, 'y': 2}]},
            {'kind': 'gesture', 'steps': [dict(hold, hold_ms=3000)] * 2},
            {'kind': 'gesture', 'steps': [dict(drag, deltas=drag['deltas'] * 8)] * 4},
            {'kind': 'gesture', 'steps': [drag] * 9},
            dict(hold, hold_ms=True),
            dict(drag, deltas=[{'dx': 0, 'dy': 0}]),
            dict(drag, deltas=[{'dx': -128, 'dy': 1}]),
        )
        for action in invalid:
            with self.subTest(action=action), self.assertRaises(ValueError):
                hid.encode_plan(action)

    def test_absolute_pointer_requires_explicit_binding_and_preserves_release_position(self):
        tap = {'kind': 'tap', 'x': 32767, 'y': 4609}
        request = dict(self.request, action=tap)
        with self.assertRaises(PermissionError):
            hid.prepare(self.binding, request)
        binding = dict(self.binding, report_modes=['pointer_absolute'])
        mode, reports, digest = hid.prepare(binding, request)
        self.assertEqual(mode, 'pointer_absolute')
        self.assertEqual([step.payload.hex() for step in reports], ['00ff7f0112', '01ff7f0112', '00ff7f0112'])
        self.assertNotEqual(digest, hid.prepare(binding, dict(request, action=dict(tap, x=16384)))[2])
        drag = {'kind': 'drag_to', 'button': 'left', 'points': [{'x': 0, 'y': 32767}, {'x': 259, 'y': 515}]}
        _, reports = hid.encode_plan(drag)
        self.assertEqual([step.payload.hex() for step in reports],
                         ['000000ff7f', '010000ff7f', '0103010302', '0003010302'])
        for x in (-1, 32768, True, 0.5):
            with self.subTest(x=x), self.assertRaises(ValueError):
                hid.prepare(binding, dict(request, action=dict(tap, x=x)))

    def test_binding_and_att_match_exact_identity(self):
        binding_file = self.root / 'binding.json'
        binding_file.write_text(json.dumps(self.binding), encoding='utf8')
        os.chmod(binding_file, 0o600)
        self.assertEqual(hid.load_binding(binding_file), self.binding)
        os.chmod(binding_file, 0o644)
        with self.assertRaises(ValueError):
            hid.load_binding(binding_file)
        self.assertEqual(hid.prepare(self.binding, self.request)[0], 'keyboard')
        with self.assertRaises(PermissionError):
            hid.prepare(dict(self.binding, report_modes=['pointer']), self.request)
        with self.assertRaises(ValueError):
            hid.prepare(self.binding, dict(self.request, phone_node_id='another-phone'))
        props = {'Adapter': '/org/bluez/hci7', 'Address': self.binding['peer_address'],
                 'Paired': True, 'Bonded': True, 'Connected': True}
        hid.validate_peer(self.binding, props, connected=True)
        for key, bad in [('Bonded', None), ('Paired', False), ('Connected', False), ('Address', 'different')]:
            with self.subTest(key=key), self.assertRaises(PermissionError):
                hid.validate_peer(self.binding, dict(props, **{key: bad}), connected=True)
        peer = '/org/bluez/hci7/dev_00_11_22_33_44_55'
        options = {'device': peer, 'link': 'LE'}
        hid.validate_att(':1.4', ':1.4', options, peer)
        for sender, candidate in [(':1.5', options), (':1.4', dict(options, link='BR/EDR')),
                                  (':1.4', dict(options, device=peer + '_other')), (':1.4', {})]:
            with self.assertRaises(PermissionError):
                hid.validate_att(sender, ':1.4', candidate, peer)

    def test_receipt_stops_retry_after_unknown_dispatch(self):
        _, _, digest = hid.prepare(self.binding, self.request)
        with hid.state_lock(self.binding) as (state, directory_fd):
            with self.assertRaises(PermissionError), hid.state_lock(self.binding):
                self.fail('two simultaneous local sessions')
            receipt = hid.Receipt(state, directory_fd, self.request['operation_id'], digest)
            self.assertEqual(receipt.read()['state'], 'not_started')
            receipt.write('.claim', 'dispatch_unknown')
            self.assertEqual(receipt.read()['state'], 'dispatch_unknown')
            with self.assertRaises(ValueError):
                hid.Receipt(state, directory_fd, self.request['operation_id'], 'different').read()
            receipt.write('.result', 'reports_queued_to_bluez', reports_queued=2)
            self.assertFalse(receipt.read()['phone_effect_verified'])
            self.assertEqual(receipt.read()['reports_queued'], 2)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in state.iterdir()))

    def test_cli_prepare_and_status_without_bluetooth(self):
        binding_file = self.root / 'binding.json'
        request_file = self.root / 'request.json'
        for path, content in ((binding_file, self.binding), (request_file, self.request)):
            path.write_text(json.dumps(content), encoding='utf8')
            path.chmod(0o600)
        command = [sys.executable, '-m', 'assistant_mesh.paired_phone_input',
                   '--binding', str(binding_file), '--request', str(request_file)]
        preview = json.loads(subprocess.check_output(command + ['prepare']))
        self.assertEqual(preview['state'], 'prepared')
        self.assertEqual(preview['report_count'], 2)
        status = json.loads(subprocess.check_output(command + ['status']))
        self.assertEqual(status['state'], 'not_started')
        self.assertEqual(status['request_sha256'], preview['request_sha256'])


class BluezReportSocketTest(unittest.TestCase):
    def setUp(self):
        try:
            from assistant_mesh import bluez_phone_hid as bluez
        except ImportError as error:
            self.skipTest(str(error))
        self.bluez = bluez

    def test_notification_fd_uses_exact_att_context_and_packet_boundaries(self):
        bluez = self.bluez
        session = SimpleNamespace(owner=':1.5', peer='/org/bluez/hci0/dev_test', channels={},
                                  reason=None, deadline=bluez.now() + 30)
        session.att = lambda sender, options: bluez.Session.att(session, sender, options)
        characteristic = bluez.Characteristic.__new__(bluez.Characteristic)
        characteristic.session = session
        characteristic.report_mode = 'keyboard'
        options = {'device': session.peer, 'link': 'LE', 'mtu': 23}
        with self.assertRaises(bluez.dbus.DBusException):
            characteristic.AcquireNotify(options, sender=':1.6')
        with self.assertRaises(bluez.dbus.DBusException):
            characteristic.AcquireNotify(dict(options, link='BR/EDR'), sender=session.owner)
        with self.assertRaises(bluez.dbus.DBusException):
            characteristic.StartNotify(sender=session.owner)
        self.assertEqual(session.channels, {})
        fd, mtu = characteristic.AcquireNotify(options, sender=session.owner)
        self.assertEqual(mtu, 23)
        channel = session.channels['keyboard']
        with bluez.socket.socket(fileno=fd.take()) as daemon, channel:
            first, release = b'\0\0\x2b' + bytes(5), bytes(8)
            channel.send(first)
            channel.send(release)
            self.assertEqual(daemon.recv(256), first)
            self.assertEqual(daemon.recv(256), release)
            self.assertFalse(bluez.Session.channel_closed(channel))
            daemon.close()
            self.assertTrue(bluez.Session.channel_closed(channel))

    def test_report_map_exports_only_the_bound_profiles(self):
        bluez = self.bluez
        for modes, descriptors, references in (
                (['pointer'], hid.POINTER_MAP, [b'\x02\x01']),
                (['pointer', 'pointer_absolute'], hid.POINTER_MAP + hid.ABSOLUTE_POINTER_MAP,
                 [b'\x02\x01', b'\x03\x01'])):
            session = SimpleNamespace(bus=None, binding={'report_modes': modes})
            with self.subTest(modes=modes), patch.object(bluez.dbus.service.Object, '__init__', return_value=None):
                app = bluez.Application(session)
                self.assertEqual(next(child.value for child in app.children if child.path.endswith('/map')),
                                 descriptors)
                self.assertEqual([child.value for child in app.children if isinstance(child, bluez.ReportReference)],
                                 references)
                if 'pointer_absolute' in modes:
                    self.assertEqual(next(child.value for child in app.children
                                          if child.path.endswith('/pointer_absolute')), b'\x00\xff\xff\xff\xff')

    def test_timed_press_releases_same_coordinate_when_session_expires(self):
        bluez = self.bluez
        binding = {'adapter': 'hci7', 'peer_address': '00:11:22:33:44:55'}
        mode, reports = hid.encode_plan(
            {'kind': 'long_press_at', 'x': 4321, 'y': 18901, 'button': 'left', 'hold_ms': 700})
        bus = Mock()
        tty = SimpleNamespace(fileno=lambda: 1)
        with (patch.object(bluez, 'now', return_value=100.0) as clock,
              patch.object(bluez.os, 'tcgetpgrp', return_value=bluez.os.getpgrp())):
            session = bluez.Session(bus, binding, mode, reports, None, tty, False)
            session.check_peer = Mock()
            session.authenticated = session.registered = session.prompted = session.claimed = True
            channel, daemon = bluez.socket.socketpair(bluez.socket.AF_UNIX, bluez.socket.SOCK_SEQPACKET)
            with channel, daemon:
                daemon.setblocking(False)
                session.channels[mode] = channel
                self.assertTrue(session.advance())
                self.assertEqual(daemon.recv(256), reports[0].payload)
                clock.return_value = 100.03
                self.assertTrue(session.advance())
                self.assertEqual(daemon.recv(256), reports[1].payload)
                clock.return_value = 100.7
                self.assertTrue(session.advance())
                with self.assertRaises(BlockingIOError):
                    daemon.recv(256)
                session.deadline = 100.75
                clock.return_value = 100.76
                self.assertFalse(session.advance())
                session.close()
                self.assertEqual(daemon.recv(256), reports[2].payload)
        self.assertEqual(session.sent, 2)
        self.assertEqual(session.reason, 'paired_phone_session_expired')
        self.assertTrue(session.release_queued)


if __name__ == '__main__':
    unittest.main()
