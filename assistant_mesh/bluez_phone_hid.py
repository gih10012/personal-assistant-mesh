"""BlueZ HID Service with a peer-specific, authenticated notification socket."""

import os
import select
import signal
import socket
import termios
import threading
import time
from concurrent.futures import Future

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from .paired_phone_input import (ABSOLUTE_POINTER_ID, ABSOLUTE_POINTER_MAP, KEYBOARD_ID, KEYBOARD_MAP,
                                 NEUTRAL, POINTER_ID, POINTER_MAP, SESSION_SECONDS, held_release,
                                 validate_att, validate_peer)
from .phone_frame_client import FrameStaleError


BLUEZ = 'org.bluez'
DEVICE = BLUEZ + '.Device1'
ADAPTER = BLUEZ + '.Adapter1'
PROPERTIES = 'org.freedesktop.DBus.Properties'
OBJECT_MANAGER = 'org.freedesktop.DBus.ObjectManager'
GATT_MANAGER = BLUEZ + '.GattManager1'
ADVERTISING_MANAGER = BLUEZ + '.LEAdvertisingManager1'
GATT_SERVICE = BLUEZ + '.GattService1'
CHARACTERISTIC = BLUEZ + '.GattCharacteristic1'
DESCRIPTOR = BLUEZ + '.GattDescriptor1'
ADVERTISEMENT = BLUEZ + '.LEAdvertisement1'
ROOT = '/org/amita/paired_phone_input'
HID = ROOT + '/hid'
AD = ROOT + '/advertisement'
READ = 'encrypt-authenticated-read'
WRITE = 'encrypt-authenticated-write'
NOTIFY = 'encrypt-authenticated-notify'
REPORT_DRAIN_SECONDS = 0.2
RELEASE_DRAIN_SECONDS = 0.05
HID_REPORTS = (
    ('keyboard', KEYBOARD_ID, KEYBOARD_MAP),
    ('pointer', POINTER_ID, POINTER_MAP),
    ('pointer_absolute', ABSOLUTE_POINTER_ID, ABSOLUTE_POINTER_MAP),
)


def uuid(number):
    return '0000%04x-0000-1000-8000-00805f9b34fb' % number


def fail(name='NotAuthorized'):
    raise dbus.exceptions.DBusException(name=BLUEZ + '.Error.' + name)


def now():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class Export(dbus.service.Object):
    def __init__(self, session, path, interface, properties):
        super().__init__(session.bus, path)
        self.session, self.path, self.interface = session, path, interface
        self.properties = dbus.Dictionary(properties, signature='sv')

    @dbus.service.method(PROPERTIES, in_signature='s', out_signature='a{sv}', sender_keyword='sender')
    def GetAll(self, interface, sender=None):
        self.session.require_owner(sender)
        if interface != self.interface:
            fail('NotSupported')
        return self.properties

    @dbus.service.method(PROPERTIES, in_signature='ss', out_signature='v', sender_keyword='sender')
    def Get(self, interface, name, sender=None):
        result = self.GetAll(interface, sender)
        if name not in result:
            fail('NotSupported')
        return result[name]


class Attribute(Export):
    def read(self, options, sender):
        self.session.att(sender, options)
        if READ not in self.properties['Flags']:
            fail('NotPermitted')
        offset = int(options.get('offset', 0))
        value = self.value
        if not 0 <= offset <= len(value):
            fail('InvalidOffset')
        # Authenticated read on this LE link proves bond possession even if
        # BlueZ restores a previously configured CCC subscription.
        self.session.authenticated = True
        return dbus.ByteArray(value[offset:])


class ReportReference(Attribute):
    def __init__(self, session, characteristic, report_id, report_type):
        super().__init__(session, characteristic.path + '/reference', DESCRIPTOR, {
            'UUID': uuid(0x2908), 'Characteristic': dbus.ObjectPath(characteristic.path),
            'Flags': dbus.Array([READ], signature='s')})
        self.value = bytes((report_id, report_type))

    @dbus.service.method(DESCRIPTOR, in_signature='a{sv}', out_signature='ay', sender_keyword='sender')
    def ReadValue(self, options, sender=None):
        return self.read(options, sender)


class Characteristic(Attribute):
    def __init__(self, session, name, number, flags, value=b'', report_mode=None):
        properties = {'UUID': uuid(number), 'Service': dbus.ObjectPath(HID),
                      'Flags': dbus.Array(flags, signature='s')}
        if report_mode:
            properties['NotifyAcquired'] = dbus.Boolean(False)
        super().__init__(session, HID + '/' + name, CHARACTERISTIC, properties)
        self.value, self.report_mode = value, report_mode

    @dbus.service.method(CHARACTERISTIC, in_signature='a{sv}', out_signature='ay', sender_keyword='sender')
    def ReadValue(self, options, sender=None):
        return self.read(options, sender)

    @dbus.service.method(CHARACTERISTIC, in_signature='aya{sv}', out_signature='', sender_keyword='sender')
    def WriteValue(self, value, options, sender=None):
        self.session.att(sender, options)
        if WRITE not in self.properties['Flags'] or options.get('prepare-authorize'):
            fail('NotPermitted')
        if options.get('offset', 0) != 0:
            fail('InvalidOffset')
        value = bytes(value)
        if len(value) != 1:
            fail('InvalidValueLength')
        if self.path.endswith('/control'):
            if value not in (b'\x00', b'\x01'):
                fail('NotSupported')
            self.session.suspended = value == b'\x00'
            if self.session.suspended and self.session.send_confirmed:
                self.session.stop('paired_phone_hid_suspended')
        else:
            self.value = value
        self.session.authenticated = True

    @dbus.service.method(CHARACTERISTIC, in_signature='a{sv}', out_signature='hq', sender_keyword='sender')
    def AcquireNotify(self, options, sender=None):
        self.session.att(sender, options)
        mtu = options.get('mtu', 0)
        if (not self.report_mode or self.report_mode in self.session.channels or not 23 <= mtu <= 517):
            fail('Failed')
        # SOCK_SEQPACKET preserves report boundaries. The other descriptor is
        # installed by BlueZ on the requesting ATT connection, not an adapter.
        local, daemon = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            local.setblocking(False)
            fd = dbus.types.UnixFd(daemon.fileno())
        except Exception:
            local.close()
            raise
        finally:
            daemon.close()
        self.session.channels[self.report_mode] = local
        return fd, dbus.UInt16(mtu)

    @dbus.service.method(CHARACTERISTIC, in_signature='', out_signature='', sender_keyword='sender')
    def StartNotify(self, sender=None):
        # This older path has no peer binding. No report is emitted through
        # PropertiesChanged or any application-wide characteristic value.
        fail('NotPermitted')

    @dbus.service.method(CHARACTERISTIC, in_signature='', out_signature='', sender_keyword='sender')
    def StopNotify(self, sender=None):
        self.session.require_owner(sender)


class Application(dbus.service.Object):
    def __init__(self, session):
        super().__init__(session.bus, ROOT)
        self.session = session
        modes = session.binding['report_modes']
        report_map = b''.join(descriptor for mode, _, descriptor in HID_REPORTS if mode in modes)
        self.children = [
            Export(session, HID, GATT_SERVICE, {'UUID': uuid(0x1812), 'Primary': dbus.Boolean(True)}),
            Characteristic(session, 'information', 0x2A4A, [READ], b'\x11\x01\x00\x00'),
            Characteristic(session, 'map', 0x2A4B, [READ], report_map),
            Characteristic(session, 'control', 0x2A4C, [WRITE, 'write-without-response']),
        ]
        for mode, report_id, _ in HID_REPORTS:
            if mode in modes:
                characteristic = Characteristic(session, mode, 0x2A4D, [READ, NOTIFY], NEUTRAL[mode], mode)
                self.children.extend((characteristic, ReportReference(session, characteristic, report_id, 1)))
        if 'keyboard' in modes:
            leds = Characteristic(session, 'keyboard_leds', 0x2A4D, [READ, WRITE, 'write-without-response'], b'\0')
            self.children.extend((leds, ReportReference(session, leds, KEYBOARD_ID, 2)))

    @dbus.service.method(OBJECT_MANAGER, in_signature='', out_signature='a{oa{sa{sv}}}', sender_keyword='sender')
    def GetManagedObjects(self, sender=None):
        self.session.require_owner(sender)
        return {child.path: {child.interface: child.properties} for child in self.children}


class Advertisement(Export):
    def __init__(self, session):
        super().__init__(session, AD, ADVERTISEMENT, {
            'Type': 'peripheral', 'ServiceUUIDs': dbus.Array([uuid(0x1812)], signature='s'),
            'Discoverable': dbus.Boolean(False), 'Timeout': dbus.UInt16(SESSION_SECONDS),
        })

    @dbus.service.method(ADVERTISEMENT, in_signature='', out_signature='', sender_keyword='sender')
    def Release(self, sender=None):
        self.session.require_owner(sender)
        self.session.stop('paired_phone_advertisement_ended')


class Session:
    def __init__(self, bus, binding, mode, reports, receipt, tty, advertise, frame_verifier=None):
        self.bus, self.binding, self.mode = bus, binding, mode
        self.reports, self.receipt, self.tty, self.advertise = reports, receipt, tty, advertise
        self.adapter = '/org/bluez/' + binding['adapter']
        self.peer = self.adapter + '/dev_' + binding['peer_address'].replace(':', '_')
        self.owner = bus.get_name_owner(BLUEZ)
        self.loop = GLib.MainLoop()
        self.deadline = now() + SESSION_SECONDS
        self.channels = {}
        self.authenticated = self.suspended = self.registered = self.advertising = False
        self.prompted = self.send_confirmed = self.claimed = self.report_attempted = False
        self.frame_verifier, self.frame_check, self.frame_deadline = frame_verifier, None, None
        self.peer_checked = False
        self.sent = 0
        self.next_report_at = 0
        self.drained_at = None
        self.pending_release = None
        self.release_drained_at = 0
        self.release_queued = None
        self.reason = None

    def require_owner(self, sender):
        if sender != self.owner:
            fail()

    def att(self, sender, options):
        try:
            validate_att(sender, self.owner, options, self.peer)
        except PermissionError:
            fail()
        if self.reason or now() >= self.deadline:
            fail('NotPermitted')

    def proxy(self, path, interface):
        return dbus.Interface(self.bus.get_object(self.owner, path, introspect=False), interface)

    def properties(self, path, interface):
        return self.proxy(path, PROPERTIES).GetAll(interface, timeout=1)

    def check_peer(self, *, connected):
        validate_peer(self.binding, self.properties(self.peer, DEVICE), connected=connected)

    def stop(self, reason):
        if self.reason is None:
            self.reason = reason
        self.loop.quit()

    def peer_changed(self, interface, changed, invalidated):
        if interface != DEVICE:
            return
        keys = {'Adapter', 'Address', 'Bonded', 'Paired', 'Connected'}
        if (keys.intersection(invalidated) or {'Adapter', 'Address'}.intersection(changed)
                or any(key in changed and not changed[key] for key in ('Bonded', 'Paired', 'Connected'))):
            self.authenticated = False
            self.stop('paired_phone_peer_binding_changed')

    def start(self):
        self.check_peer(connected=not self.advertise)
        adapter = self.properties(self.adapter, ADAPTER)
        if not adapter.get('Powered'):
            raise PermissionError('paired_phone_powered_adapter_required')
        if self.advertise and adapter.get('Pairable') != False:
            raise PermissionError('paired_phone_existing_bond_and_non_pairable_adapter_required')

        self.bus.add_signal_receiver(self.peer_changed, signal_name='PropertiesChanged',
                                     dbus_interface=PROPERTIES, bus_name=self.owner, path=self.peer)
        self.bus.add_signal_receiver(self.adapter_changed, signal_name='PropertiesChanged',
                                     dbus_interface=PROPERTIES, bus_name=self.owner, path=self.adapter)
        self.bus.add_signal_receiver(self.interfaces_removed, signal_name='InterfacesRemoved',
                                     dbus_interface=OBJECT_MANAGER, bus_name=self.owner, path='/')
        self.bus.watch_name_owner(BLUEZ, self.bluez_changed)
        self.application = Application(self)
        self.advertisement = Advertisement(self) if self.advertise else None
        self.proxy(self.adapter, GATT_MANAGER).RegisterApplication(
            dbus.ObjectPath(ROOT), dbus.Dictionary({}, signature='sv'), reply_handler=self.gatt_ready,
            error_handler=self.registration_error, timeout=3)
        GLib.timeout_add(20, self.tick)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self.cancel)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self.cancel)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGHUP, self.cancel)
        self.loop.run()

    def bluez_changed(self, owner):
        if owner != self.owner:
            self.authenticated = False
            self.stop('paired_phone_bluez_owner_changed')

    def adapter_changed(self, interface, changed, invalidated):
        if interface != ADAPTER:
            return
        if 'Powered' in invalidated or 'Powered' in changed and not changed['Powered']:
            self.authenticated = False
            self.stop('paired_phone_adapter_changed')
        elif self.advertise and ('Pairable' in invalidated or changed.get('Pairable')):
            self.stop('paired_phone_adapter_changed')

    def interfaces_removed(self, path, interfaces):
        if path in (self.adapter, self.peer) and {ADAPTER, DEVICE}.intersection(interfaces):
            self.authenticated = False
            self.stop('paired_phone_peer_binding_removed')

    def gatt_ready(self):
        self.registered = True
        if self.advertise and self.reason is None and now() < self.deadline:
            self.proxy(self.adapter, ADVERTISING_MANAGER).RegisterAdvertisement(
                dbus.ObjectPath(AD), dbus.Dictionary({}, signature='sv'), reply_handler=self.advertisement_ready,
                error_handler=self.registration_error, timeout=3)

    def advertisement_ready(self):
        self.advertising = True

    def registration_error(self, error):
        self.stop('paired_phone_bluez_registration_failed:' + error.get_dbus_name())

    def cancel(self):
        self.stop('paired_phone_owner_cancelled')
        return GLib.SOURCE_REMOVE

    def verify_in_background(self, check):
        # Only the GLib thread can claim an operation or send a report. A late
        # Source reply can only populate this Future with a deadline or reason.
        try:
            result = self.frame_verifier(now), None
        except FrameStaleError:
            result = None, 'paired_phone_frame_stale'
        except Exception:
            result = None, 'paired_phone_frame_unavailable'
        check.set_result(result)

    def peer_check_failed(self, _error):
        self.stop('paired_phone_connection_required')

    def guarded_peer_ready(self, properties):
        try:
            validate_peer(self.binding, properties, connected=True)
        except PermissionError as error:
            self.stop(str(error))
            return
        if self.advertise:
            self.proxy(self.adapter, PROPERTIES).GetAll(
                ADAPTER, reply_handler=self.guarded_adapter_ready, error_handler=self.peer_check_failed, timeout=1)
        else:
            self.peer_checked = True

    def guarded_adapter_ready(self, properties):
        if properties.get('Pairable') != False:
            self.stop('paired_phone_adapter_became_pairable')
        else:
            self.peer_checked = True

    def confirm_send(self):
        if not self.send_confirmed:
            if not select.select([self.tty], [], [], 0)[0]:
                return False
            if self.tty.read(128) != b'SEND\n':
                self.stop('paired_phone_owner_cancelled')
                return False
            self.send_confirmed = True
            if self.frame_verifier is None:
                self.check_peer(connected=True)
                if self.advertise and self.properties(self.adapter, ADAPTER).get('Pairable') != False:
                    raise PermissionError('paired_phone_adapter_became_pairable')
                return True
            self.frame_check = Future()
            threading.Thread(target=self.verify_in_background, args=(self.frame_check,), daemon=True).start()
            return False
        if self.frame_deadline is None and self.frame_check.done():
            self.frame_deadline, reason = self.frame_check.result()
            if reason is not None:
                self.stop(reason)
                return False
            self.proxy(self.peer, PROPERTIES).GetAll(
                DEVICE, reply_handler=self.guarded_peer_ready, error_handler=self.peer_check_failed, timeout=1)
        return self.peer_checked

    def tick(self):
        try:
            return self.advance()
        except (OSError, ValueError, dbus.exceptions.DBusException) as error:
            self.stop(str(error) if isinstance(error, PermissionError) else 'paired_phone_local_dispatch_error')
            return GLib.SOURCE_REMOVE

    def advance(self):
        if self.reason is not None:
            return GLib.SOURCE_REMOVE
        current = now()
        if current >= self.deadline:
            self.stop('paired_phone_session_expired' if self.prompted else 'paired_phone_host_subscription_required')
            return GLib.SOURCE_REMOVE
        if os.tcgetpgrp(self.tty.fileno()) != os.getpgrp():
            self.stop('paired_phone_foreground_terminal_required')
            return GLib.SOURCE_REMOVE
        channel = self.channels.get(self.mode)
        if channel is not None and self.channel_closed(channel):
            self.authenticated = False
            self.stop('paired_phone_notify_socket_closed')
            return GLib.SOURCE_REMOVE
        if self.frame_deadline is not None and not self.report_attempted and current >= self.frame_deadline:
            self.stop('paired_phone_frame_stale')
            return GLib.SOURCE_REMOVE
        if (not self.registered or self.advertise and not self.advertising or channel is None
                or not self.authenticated or self.suspended):
            return GLib.SOURCE_CONTINUE
        if not self.prompted:
            termios.tcflush(self.tty.fileno(), termios.TCIFLUSH)
            self.tty.write(b'Bonded HID host ready. Check the target on the phone and type SEND. ')
            self.prompted = True
            return GLib.SOURCE_CONTINUE
        if not self.claimed:
            if not self.confirm_send():
                return GLib.SOURCE_REMOVE if self.reason else GLib.SOURCE_CONTINUE
            if self.frame_deadline is not None and now() >= self.frame_deadline:
                self.stop('paired_phone_frame_stale')
                return GLib.SOURCE_REMOVE
            report_seconds = sum(step.delay_ms for step in self.reports[1:]) / 1000
            if now() + report_seconds + REPORT_DRAIN_SECONDS >= self.deadline:
                self.stop('paired_phone_session_time_insufficient')
                return GLib.SOURCE_REMOVE
            self.receipt.write('.claim', 'dispatch_unknown')
            self.claimed = True
            if self.frame_verifier is not None:
                # Give queued cancel/disconnect handlers a turn after fsync.
                # The next tick rechecks both deadlines and the peer channel.
                return GLib.SOURCE_CONTINUE
        current = now()
        if self.sent < len(self.reports):
            if current >= self.deadline:
                self.stop('paired_phone_session_expired')
                return GLib.SOURCE_REMOVE
            if self.frame_deadline is not None and not self.report_attempted and current >= self.frame_deadline:
                self.stop('paired_phone_frame_stale')
                return GLib.SOURCE_REMOVE
            if current < self.next_report_at:
                return GLib.SOURCE_CONTINUE
            report = self.reports[self.sent].payload
            release = held_release(self.mode, report)
            if self.pending_release is None:
                self.pending_release = release
            self.report_attempted = True
            if channel.send(report) != len(report):
                raise OSError('paired_phone_short_report_write')
            sent_at = now()
            if self.pending_release is not None and release is None:
                self.release_drained_at = sent_at + RELEASE_DRAIN_SECONDS
            self.pending_release = release
            self.sent += 1
            if self.sent == len(self.reports):
                self.drained_at = sent_at + REPORT_DRAIN_SECONDS
            else:
                self.next_report_at = sent_at + self.reports[self.sent].delay_ms / 1000
        elif current >= self.drained_at:
            self.stop('reports_queued_to_bluez')
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def channel_closed(channel):
        poller = select.poll()
        poller.register(channel, select.POLLHUP | select.POLLERR | select.POLLRDHUP)
        return bool(poller.poll(0))

    def release_held(self):
        channel = self.channels.get(self.mode)
        if not self.authenticated or channel is None:
            return False
        try:
            self.check_peer(connected=True)
            poller = select.poll()
            poller.register(channel, select.POLLOUT)
            if not poller.poll(80) or self.channel_closed(channel):
                return False
            if channel.send(self.pending_release) != len(self.pending_release):
                return False
            time.sleep(RELEASE_DRAIN_SECONDS)
            return True
        except (OSError, dbus.exceptions.DBusException):
            return False

    def close(self):
        if self.pending_release is not None:
            # Release on the acquired peer channel only, including on deadline.
            self.release_queued = self.release_held()
        elif self.authenticated:
            remaining = self.release_drained_at - now()
            if remaining > 0:
                time.sleep(remaining)
        for channel in self.channels.values():
            channel.close()
        # BlueZ cleans up exported services and advertising when this private
        # connection closes, including a late registration acknowledgement.
        self.bus.close()


def run_session(binding, mode, reports, receipt, tty, advertise, frame_verifier=None):
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = None
    session = None
    try:
        bus = dbus.SystemBus(private=True)
        session = Session(bus, binding, mode, reports, receipt, tty, advertise, frame_verifier)
        session.start()
    except dbus.exceptions.DBusException as error:
        reason = 'paired_phone_bluez_unavailable:' + error.get_dbus_name()
        if session is None:
            return dict(receipt.identity, state='not_sent', reason=reason)
        session.stop(reason)
    finally:
        if session is not None:
            session.close()
        elif bus is not None:
            bus.close()
    if frame_verifier is not None and session.send_confirmed and not session.report_attempted:
        return receipt.write('.result', 'not_sent', reason=session.reason, reports_queued=0)
    if session.claimed:
        state = session.reason if session.reason == 'reports_queued_to_bluez' else 'dispatch_unknown'
        details = {'reason': session.reason, 'reports_queued': session.sent}
        if frame_verifier is not None:
            details['frame_verified_before_send'] = True
        if session.release_queued is not None:
            details['release_queued_to_bluez'] = session.release_queued
        return receipt.write('.result', state, **details)
    return dict(receipt.identity, state='not_sent', reason=session.reason)
