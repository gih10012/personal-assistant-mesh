"""Opt-in, owned SSH SOCKS egress; never changes the host's native network.

This is a transport primitive, not an admission/authorization registry. Existing
owner SSH authentication stays in its private configuration. Each invocation
owns a foreground process and only terminates/reaps that process.
"""

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class EgressError(RuntimeError):
    """A safe diagnostic code; never contains SSH/curl raw stderr or secrets."""


def _bounded_capture(command, maximum, timeout):
    """Read at most maximum+1 bytes; old curl chunked bodies cannot fill disk."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = []
    failures = []

    def read():
        try:
            output.append(process.stdout.read(maximum + 1))
            if len(output[0]) > maximum and process.poll() is None:
                process.kill()
        except OSError:
            failures.append(True)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    timed_out = False
    try:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait(timeout=3)
        reader.join(timeout=3)
        if reader.is_alive():
            raise EgressError("egress_probe_reader_unavailable")
        if timed_out:
            raise EgressError("egress_probe_timeout")
        if failures:
            raise EgressError("egress_probe_read_failed")
        if not output or len(output[0]) > maximum:
            raise EgressError("egress_probe_body_limit")
        return process.returncode, output[0]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stdout.close()


def _private_file(path):
    if not isinstance(path, (str, Path)) or (isinstance(path, str) and "\0" in path):
        raise EgressError("private_absolute_file_required")
    value = Path(path)
    if not value.is_absolute() or value.is_symlink():
        raise EgressError("private_absolute_file_required")
    for parent in value.parents:
        if parent.is_symlink():
            raise EgressError("private_absolute_file_required")
    try:
        info = value.stat()
    except OSError:
        raise EgressError("private_file_unavailable")
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise EgressError("private_owned_file_required")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise EgressError("private_owned_file_required")
    return value


class EgressConfig:
    def __init__(self, ssh_destination, ssh_config, port=0,
                 readiness_seconds=15, max_seconds=900, ssh_binary="ssh"):
        if not isinstance(ssh_destination, str) or not re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", ssh_destination):
            raise EgressError("invalid_ssh_destination")
        self.ssh_destination = ssh_destination
        self.ssh_config = str(_private_file(ssh_config))
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise EgressError("invalid_loopback_port")
        if isinstance(readiness_seconds, bool) or not isinstance(readiness_seconds, (int, float)):
            raise EgressError("invalid_readiness_seconds")
        if not 0 < readiness_seconds <= 60:
            raise EgressError("invalid_readiness_seconds")
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)):
            raise EgressError("invalid_max_seconds")
        if not 0 < max_seconds <= 86400:
            raise EgressError("invalid_max_seconds")
        if not isinstance(ssh_binary, str) or "\0" in ssh_binary or (ssh_binary != "ssh" and not Path(ssh_binary).is_absolute()):
            raise EgressError("invalid_ssh_binary")
        self.port = port
        self.readiness_seconds = float(readiness_seconds)
        self.max_seconds = float(max_seconds)
        self.ssh_binary = ssh_binary

    @classmethod
    def load(cls, path):
        try:
            config = json.loads(_private_file(path).read_text(encoding="utf-8"))
        except (ValueError, UnicodeError, OSError):
            raise EgressError("invalid_private_egress_config")
        expected = {"ssh_destination", "ssh_config", "port", "readiness_seconds", "max_seconds", "ssh_binary"}
        if not isinstance(config, dict) or set(config) - expected:
            raise EgressError("unexpected_egress_config_fields")
        if "ssh_destination" not in config or "ssh_config" not in config:
            raise EgressError("incomplete_egress_config")
        return cls(**config)


class SshSocksEgress:
    """A single owned tunnel. Use with-context or close(); never PID adoption.

    max_seconds bounds each API operation and the CLI hold/run lifetime. This
    class does not install a background service or monitor abandoned callers.
    """
    def __init__(self, config):
        self.config = config
        self.process = None
        self.port = None
        self.started_at = None
        self.deadline = None
        self._directory = None
        self._stderr = None
        self._closed = False

    def _base_command(self):
        return [self.config.ssh_binary, "-F", self.config.ssh_config,
                "-o", "BatchMode=yes", "-o", "ControlMaster=no",
                "-o", "ControlPath=none", "-o", "ControlPersist=no",
                "-o", "ForkAfterAuthentication=no", "-o", "PermitLocalCommand=no",
                "-o", "LocalCommand=none", "-o", "RequestTTY=no",
                "-o", "GatewayPorts=no", "-o", "ForwardAgent=no",
                "-o", "ForwardX11=no", "-o", "Tunnel=no"]

    def _check_profile(self):
        # Reject inherited forwards rather than touching production listeners.
        # ssh -G resolves aliases/includes without connecting or executing the
        # resulting ProxyCommand/LocalCommand. Do not log its private output.
        try:
            resolved = subprocess.run(
                self._base_command() + ["-G", self.config.ssh_destination],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(10, self.config.readiness_seconds), check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise EgressError("ssh_profile_resolution_failed")
        if resolved.returncode:
            raise EgressError("ssh_profile_resolution_failed")
        for line in resolved.stdout.decode("utf-8", "replace").splitlines():
            if line.split(" ", 1)[0].lower() in {"localforward", "remoteforward", "dynamicforward"}:
                raise EgressError("ssh_profile_has_inherited_forward")

    def start(self):
        if self.process is not None or self._closed:
            raise EgressError("egress_instance_not_fresh")
        self._check_profile()
        # Port zero picks a currently unused local port. ExitOnForwardFailure
        # and owned-process checks catch bind loss/race; no reuse of a listener.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
                reserved.bind(("127.0.0.1", self.config.port))
                self.port = reserved.getsockname()[1]
        except OSError:
            raise EgressError("loopback_port_unavailable")
        self._directory = tempfile.TemporaryDirectory(prefix="mesh-egress-")
        self._stderr = open(str(Path(self._directory.name) / "ssh.stderr"), "xb")
        os.chmod(self._stderr.name, 0o600)
        command = self._base_command() + [
            "-N", "-T", "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=%d" % max(1, min(10, int(self.config.readiness_seconds))),
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-D", "127.0.0.1:%d" % self.port, self.config.ssh_destination]
        try:
            try:
                self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=self._stderr)
            except OSError:
                raise EgressError("ssh_egress_launch_failed")
            self.started_at = time.time()
            self.deadline = time.monotonic() + self.config.max_seconds
            ready_deadline = min(self.deadline, time.monotonic() + self.config.readiness_seconds)
            while time.monotonic() < ready_deadline:
                if self.process.poll() is not None:
                    raise EgressError("ssh_egress_exited_before_ready")
                if self._socks_ready(min(0.2, ready_deadline - time.monotonic())):
                    if self.process.poll() is None:
                        return self
                time.sleep(0.03)
            raise EgressError("ssh_egress_readiness_timeout")
        except BaseException:
            self.close()
            raise

    def _socks_ready(self, timeout):
        if timeout <= 0:
            return False
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as conn:
                conn.sendall(b"\x05\x01\x00")
                response = b""
                while len(response) < 2:
                    part = conn.recv(2 - len(response))
                    if not part:
                        return False
                    response += part
                return response == b"\x05\x00"
        except OSError:
            return False

    def _remaining(self):
        if self._closed or self.process is None or self.process.poll() is not None:
            raise EgressError("ssh_egress_unavailable")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.close()
            raise EgressError("ssh_egress_expired")
        return remaining

    @property
    def proxy_url(self):
        self._remaining()
        # socks5h means target DNS is resolved by the remote SSH endpoint.
        return "socks5h://127.0.0.1:%d" % self.port

    def child_environment(self, environment=None):
        env = dict(os.environ if environment is None else environment)
        value = self.proxy_url
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = value
        # An inherited NO_PROXY must not silently bypass this selected path.
        env["NO_PROXY"] = env["no_proxy"] = ""
        return env

    def status(self):
        self._remaining()
        return {"transport": "ssh-socks5h", "bind_scope": "loopback_only",
                "dns_scope": "remote", "owned_process_running": True,
                "expires_in_seconds": max(0, round(self.deadline - time.monotonic(), 3)),
                "authentication_verified": False, "model_inference_verified": False,
                "global_network_changed": False}

    def probe(self, url, method="GET", timeout=20, max_bytes=1048576, curl_binary="curl"):
        """Bounded unauthenticated TLS/HTTP observation; no redirects or .curlrc.

        This deliberately does not forward browser sessions/API credentials.
        Response content remains private; only metadata/hash is returned.
        """
        target = urlsplit(url)
        if target.scheme != "https" or not target.hostname or target.username or target.password:
            raise EgressError("probe_requires_unauthenticated_https_target")
        if method not in {"GET", "HEAD"}:
            raise EgressError("invalid_probe_method")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 16777216:
            raise EgressError("invalid_probe_max_bytes")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 60:
            raise EgressError("invalid_probe_timeout")
        bound = min(float(timeout), self._remaining())
        marker = b"\nMESH_EGRESS_METADATA:"
        command = [curl_binary, "--disable", "--silent", "--show-error",
                   "--proxy", self.proxy_url, "--noproxy", "", "--proto", "=https",
                   "--connect-timeout", str(min(10, bound)), "--max-time", str(bound),
                   "--max-filesize", str(max_bytes), "--output", "-",
                   "--write-out", marker.decode("ascii") + "%{http_code} %{ssl_verify_result}"]
        if method == "HEAD":
            command += ["--head"]
        command += ["--url", url]
        began = time.monotonic()
        try:
            returncode, output = _bounded_capture(command, max_bytes + 64, bound + 1)
        except OSError:
            raise EgressError("egress_probe_client_unavailable")
        pieces = output.rsplit(marker, 1)
        data = pieces[0]
        fields = pieces[1].decode("ascii", "replace").strip().split() if len(pieces) == 2 else []
        http_status = int(fields[0]) if len(fields) == 2 and fields[0].isdigit() else 0
        tls_result = int(fields[1]) if len(fields) == 2 and fields[1].isdigit() else None
        if len(data) > max_bytes:
            raise EgressError("egress_probe_body_limit")
        # Check this owned process again; another listener is never recovery.
        self._remaining()
        return {"transport": "ssh-socks5h", "method": method,
                "target_origin": "https://" + target.netloc, "http_status": http_status,
                "tls_verify_result": tls_result, "curl_exit_code": returncode,
                "response_bytes": len(data), "response_sha256": hashlib.sha256(data).hexdigest(),
                "duration_seconds": round(time.monotonic() - began, 3),
                "tls_http_reachable": returncode == 0 and tls_result == 0 and http_status != 0,
                "authentication_verified": False, "model_inference_verified": False,
                "redirects_followed": False}

    def run(self, command):
        if not command or not all(isinstance(item, str) for item in command):
            raise EgressError("invalid_child_command")
        # No shell interpolation; only this child gets the selected proxy.
        try:
            return subprocess.run(command, env=self.child_environment(),
                                  timeout=self._remaining(), check=False).returncode
        except subprocess.TimeoutExpired:
            raise EgressError("egress_child_timeout")
        except OSError:
            raise EgressError("egress_child_unavailable")

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
                else:
                    self.process.wait(timeout=3)
        finally:
            if self._stderr is not None:
                self._stderr.close()
            if self._directory is not None:
                self._directory.cleanup()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="private owner configuration, never credentials inline")
    commands = parser.add_subparsers(dest="action")
    probe = commands.add_parser("probe", help="unauthenticated HTTPS observation through owned VPS path")
    probe.add_argument("url")
    probe.add_argument("--method", choices=("GET", "HEAD"), default="GET")
    child = commands.add_parser("run", help="select egress for this one command, no global environment changes")
    child.add_argument("command", nargs=argparse.REMAINDER)
    commands.add_parser("hold", help="foreground path, bounded by private max_seconds; interrupt to close")
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error("an action is required")
    try:
        with SshSocksEgress(EgressConfig.load(args.config)) as egress:
            if args.action == "probe":
                result = egress.probe(args.url, method=args.method)
                print(json.dumps(result, sort_keys=True))
                return 0 if result["tls_http_reachable"] else 1
            if args.action == "run":
                command = args.command[1:] if args.command[:1] == ["--"] else args.command
                return egress.run(command)
            print(json.dumps(dict(egress.status(), proxy_url=egress.proxy_url), sort_keys=True), flush=True)
            while True:
                remaining = egress._remaining()
                time.sleep(min(0.25, remaining))
    except KeyboardInterrupt:
        return 130
    except EgressError as error:
        if str(error) == "ssh_egress_expired" and args.action == "hold":
            return 0
        print(json.dumps({"error": str(error), "credentials_or_raw_stderr_disclosed": False}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
