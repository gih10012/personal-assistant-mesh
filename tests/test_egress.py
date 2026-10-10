import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from assistant_mesh.egress import EgressConfig, EgressError, SshSocksEgress, _bounded_capture


class OwnedProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = 0

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited += 1
        return self.returncode


class EgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.ssh_config = self.root / "ssh-config"
        self.ssh_config.write_text("Host owner-vps\n  HostName example.invalid\n", encoding="utf-8")
        self.ssh_config.chmod(0o600)

    def tearDown(self):
        self.directory.cleanup()

    def config(self, **changes):
        fields = {"ssh_destination": "owner-vps", "ssh_config": str(self.ssh_config)}
        fields.update(changes)
        return EgressConfig(**fields)

    def running(self):
        egress = SshSocksEgress(self.config())
        egress.process = OwnedProcess()
        egress.port = 32123
        egress.deadline = time.monotonic() + 100
        return egress

    def test_private_config_load_rejects_inline_credentials_and_unknown_fields(self):
        path = self.root / "egress.json"
        value = {"ssh_destination": "owner-vps", "ssh_config": str(self.ssh_config)}
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        self.assertEqual("owner-vps", EgressConfig.load(path).ssh_destination)
        for field in ("password", "api_key", "bind_host", "identity_contents"):
            path.write_text(json.dumps(dict(value, **{field: "PRIVATE-NOT-OUTPUT"})), encoding="utf-8")
            with self.assertRaisesRegex(EgressError, "unexpected_egress_config_fields"):
                EgressConfig.load(path)

    def test_private_config_file_permissions_symlinks_and_missing(self):
        self.ssh_config.chmod(0o644)
        with self.assertRaisesRegex(EgressError, "private_owned_file_required"):
            self.config()
        self.ssh_config.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.ssh_config)
        with self.assertRaisesRegex(EgressError, "private_absolute_file_required"):
            self.config(ssh_config=str(link))
        with self.assertRaisesRegex(EgressError, "private_file_unavailable"):
            self.config(ssh_config=str(self.root / "missing"))

    def test_config_requires_finite_bounds_and_option_safe_destination(self):
        for field, values in {
            "port": [-1, 65536, True, "123"],
            "readiness_seconds": [0, -1, 61, True, float("nan")],
            "max_seconds": [0, -1, 86401, True, float("inf")],
            "ssh_destination": ["-oProxyCommand=bad", "with space", "x\nsecret", ""],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(EgressError):
                    self.config(**{field: value})

    def test_inherited_forwards_are_rejected_without_starting_tunnel(self):
        for field in ("localforward", "remoteforward", "dynamicforward"):
            resolved = subprocess.CompletedProcess([], 0, stdout=(field + " private-target\n").encode())
            with mock.patch("assistant_mesh.egress.subprocess.run", return_value=resolved), \
                    mock.patch("assistant_mesh.egress.subprocess.Popen") as spawn:
                with self.assertRaisesRegex(EgressError, "ssh_profile_has_inherited_forward"):
                    SshSocksEgress(self.config()).start()
                spawn.assert_not_called()

    def test_profile_failure_is_safe_code_not_stderr(self):
        result = subprocess.CompletedProcess([], 1, stdout=b"PRIVATE-PROXY")
        with mock.patch("assistant_mesh.egress.subprocess.run", return_value=result):
            with self.assertRaisesRegex(EgressError, "^ssh_profile_resolution_failed$"):
                SshSocksEgress(self.config()).start()

    def test_started_tunnel_is_loopback_foreground_and_only_owned_process_is_closed(self):
        process = OwnedProcess()
        profile = subprocess.CompletedProcess([], 0, stdout=b"hostname example.invalid\n")
        with mock.patch("assistant_mesh.egress.subprocess.run", return_value=profile), \
                mock.patch("assistant_mesh.egress.subprocess.Popen", return_value=process) as spawn, \
                mock.patch.object(SshSocksEgress, "_socks_ready", return_value=True):
            with SshSocksEgress(self.config()) as egress:
                command = spawn.call_args[0][0]
                self.assertEqual("127.0.0.1:%d" % egress.port, command[command.index("-D") + 1])
                for option in ("BatchMode=yes", "ControlMaster=no", "ControlPath=none",
                               "ForkAfterAuthentication=no", "ExitOnForwardFailure=yes"):
                    self.assertIn(option, command)
                self.assertNotIn("-f", command)
                self.assertEqual(0o700, Path(egress._directory.name).stat().st_mode & 0o777)
                self.assertEqual(0o600, Path(egress._stderr.name).stat().st_mode & 0o777)
                self.assertFalse(egress.status()["global_network_changed"])
                runtime_path = egress._directory.name
            self.assertEqual(1, process.terminated)
            self.assertEqual(1, process.waited)
            self.assertFalse(Path(runtime_path).exists())
            egress.close()
            self.assertEqual(1, process.terminated)

    def test_existing_listener_is_not_adopted_or_terminated(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            config = self.config(port=listener.getsockname()[1])
            with mock.patch.object(SshSocksEgress, "_check_profile"), \
                    mock.patch("assistant_mesh.egress.subprocess.Popen") as spawn:
                with self.assertRaisesRegex(EgressError, "loopback_port_unavailable"):
                    SshSocksEgress(config).start()
                spawn.assert_not_called()
            self.assertGreater(listener.fileno(), -1)

    def test_readiness_timeout_terminates_and_reaps_owned_ssh(self):
        process = OwnedProcess()
        with mock.patch.object(SshSocksEgress, "_check_profile"), \
                mock.patch("assistant_mesh.egress.subprocess.Popen", return_value=process), \
                mock.patch.object(SshSocksEgress, "_socks_ready", return_value=False):
            with self.assertRaisesRegex(EgressError, "ssh_egress_readiness_timeout"):
                SshSocksEgress(self.config(readiness_seconds=0.01)).start()
        self.assertEqual(1, process.terminated)
        self.assertEqual(1, process.waited)

    def test_ssh_exit_before_ready_does_not_kill_unrelated_pid(self):
        process = OwnedProcess()
        process.returncode = 255
        with mock.patch.object(SshSocksEgress, "_check_profile"), \
                mock.patch("assistant_mesh.egress.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(EgressError, "ssh_egress_exited_before_ready"):
                SshSocksEgress(self.config()).start()
        self.assertEqual(0, process.terminated)
        self.assertEqual(1, process.waited)

    def test_socks_readiness_handles_split_response(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            egress = self.running()
            egress.port = listener.getsockname()[1]
            observed = []

            def serve():
                with listener.accept()[0] as connection:
                    observed.append(connection.recv(3))
                    connection.sendall(b"\x05")
                    time.sleep(0.01)
                    connection.sendall(b"\x00")

            worker = threading.Thread(target=serve)
            worker.start()
            self.assertTrue(egress._socks_ready(1))
            worker.join(timeout=1)
            self.assertEqual([b"\x05\x01\x00"], observed)
            egress.close()

    def test_proxy_environment_only_changes_copy_and_clears_bypass(self):
        original = {"ALL_PROXY": "old-private-proxy", "NO_PROXY": "*", "UNCHANGED": "yes"}
        egress = self.running()
        changed = egress.child_environment(original)
        self.assertEqual("old-private-proxy", original["ALL_PROXY"])
        self.assertEqual("*", original["NO_PROXY"])
        for key in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
            self.assertEqual("socks5h://127.0.0.1:32123", changed[key])
        self.assertEqual("", changed["NO_PROXY"])
        self.assertEqual("yes", changed["UNCHANGED"])
        egress.close()

    def test_expired_transport_is_not_reused(self):
        egress = self.running()
        egress.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(EgressError, "ssh_egress_expired"):
            egress.status()
        self.assertEqual(1, egress.process.terminated)
        with self.assertRaisesRegex(EgressError, "ssh_egress_unavailable"):
            egress.proxy_url

    def test_http_403_401_are_only_tls_http_observations(self):
        for status in (403, 401):
            egress = self.running()
            body = b"private body not returned"
            raw = body + ("\nMESH_EGRESS_METADATA:%d 0" % status).encode()
            with mock.patch("assistant_mesh.egress._bounded_capture", return_value=(0, raw)) as capture:
                report = egress.probe("https://example.invalid/no-credentials")
            command = capture.call_args[0][0]
            self.assertEqual("--disable", command[1])
            self.assertNotIn("--location", command)
            self.assertNotIn("--insecure", command)
            self.assertIn("socks5h://127.0.0.1:32123", command)
            self.assertTrue(report["tls_http_reachable"])
            self.assertFalse(report["authentication_verified"])
            self.assertFalse(report["model_inference_verified"])
            self.assertEqual(hashlib.sha256(body).hexdigest(), report["response_sha256"])
            self.assertNotIn("private body", json.dumps(report))
            egress.close()

    def test_probe_rejects_auth_urls_non_tls_and_side_effect_methods(self):
        egress = self.running()
        for url in ("http://example.invalid", "https://user:password@example.invalid", "https:///bad"):
            with self.assertRaisesRegex(EgressError, "probe_requires_unauthenticated_https_target"):
                egress.probe(url)
        with self.assertRaisesRegex(EgressError, "invalid_probe_method"):
            egress.probe("https://example.invalid", method="POST")
        egress.close()

    def test_probe_malformed_metadata_tls_error_and_body_limit_are_not_success(self):
        egress = self.running()
        for exit_code, raw in ((60, b"\nMESH_EGRESS_METADATA:000 20"), (0, b"missing metadata")):
            with mock.patch("assistant_mesh.egress._bounded_capture", return_value=(exit_code, raw)):
                self.assertFalse(egress.probe("https://example.invalid")["tls_http_reachable"])
        with mock.patch("assistant_mesh.egress._bounded_capture", return_value=(0, b"too-long\nMESH_EGRESS_METADATA:200 0")):
            with self.assertRaisesRegex(EgressError, "egress_probe_body_limit"):
                egress.probe("https://example.invalid", max_bytes=3)
        egress.close()

    def test_run_passes_only_selected_child_env_no_shell(self):
        egress = self.running()
        completed = subprocess.CompletedProcess([], 7)
        with mock.patch("assistant_mesh.egress.subprocess.run", return_value=completed) as run:
            self.assertEqual(7, egress.run(["tool", "literal;$NOT_EXPANDED"]))
            self.assertNotIn("shell", run.call_args[1])
            self.assertEqual("literal;$NOT_EXPANDED", run.call_args[0][0][1])
            self.assertEqual(egress.proxy_url, run.call_args[1]["env"]["HTTPS_PROXY"])
            self.assertGreater(run.call_args[1]["timeout"], 0)
        egress.close()

    def test_bounded_capture_actual_child_limits_memory_and_timeout(self):
        with self.assertRaisesRegex(EgressError, "egress_probe_body_limit"):
            _bounded_capture([sys.executable, "-c", "import sys; sys.stdout.write('x'*10000)"], 10, 2)
        code, body = _bounded_capture([sys.executable, "-c", "print('ok', end='')"], 10, 2)
        self.assertEqual((0, b"ok"), (code, body))
        with self.assertRaisesRegex(EgressError, "egress_probe_timeout"):
            _bounded_capture([sys.executable, "-c", "import time; time.sleep(10)"], 10, 0.05)


if __name__ == "__main__":
    unittest.main()
