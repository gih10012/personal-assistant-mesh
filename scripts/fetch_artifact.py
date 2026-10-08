"""Optional, credential-free parallel fetch of a caller-verified public artifact.

This is an additional transport capability, not a native-network policy or an
installer. The caller supplies the URL, byte size, and an already verified
official SHA256; no release, model, authentication, or configuration is selected.
"""
import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


CHUNK_SIZE = 64 * 1024


class ArtifactFetchError(ValueError):
    """Stable error codes deliberately exclude URLs and response headers."""


def _url(url, allow_loopback_http=False, initial=False):
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ArtifactFetchError('artifact_public_https_url_required')
    try:
        value = urllib.parse.urlsplit(url)
        port = value.port
        host = value.hostname
    except ValueError:
        raise ArtifactFetchError('artifact_public_https_url_required')
    if not host or value.username is not None or value.password is not None or value.fragment:
        raise ArtifactFetchError('artifact_public_https_url_required')
    # Initial signed/token-bearing URLs are not a supported credential channel.
    # Official release redirects may contain their own short-lived signatures.
    if initial and value.query:
        raise ArtifactFetchError('artifact_initial_query_not_supported')
    if value.scheme == 'https' and (port is None or 0 < port < 65536):
        return value
    if allow_loopback_http and value.scheme == 'http' and (port is None or 0 < port < 65536):
        try:
            if ipaddress.ip_address(host).is_loopback:
                return value
        except ValueError:
            pass
    raise ArtifactFetchError('artifact_public_https_url_required')


def _remaining(deadline, stopped=None):
    if stopped is not None and stopped.is_set():
        raise ArtifactFetchError('artifact_fetch_cancelled')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ArtifactFetchError('artifact_fetch_deadline')
    return remaining


def _proxy_endpoint(proxy_url):
    if proxy_url is None:
        return None
    if (not isinstance(proxy_url, str) or not proxy_url
            or any(ord(char) <= 32 or ord(char) == 127 for char in proxy_url)):
        raise ArtifactFetchError('artifact_credential_free_http_proxy_required')
    try:
        value = urllib.parse.urlsplit(proxy_url)
        port, host = value.port, value.hostname
    except ValueError:
        raise ArtifactFetchError('artifact_credential_free_http_proxy_required') from None
    if value.scheme not in ('http', 'https'):
        raise ArtifactFetchError('artifact_proxy_protocol_not_supported')
    if (not host or value.username is not None or value.password is not None
            or '?' in proxy_url or '#' in proxy_url or value.path not in ('', '/')
            or (port is not None and not 0 < port < 65536)):
        raise ArtifactFetchError('artifact_credential_free_http_proxy_required')
    return value


class _ExplicitProxy(urllib.request.ProxyHandler):
    """Per-fetch mapping without environment bypass or proxy authentication.

    stdlib ProxyHandler.proxy_open consults NO_PROXY even with an explicit
    mapping. Keep its request-routing behavior, but never consult that branch
    or synthesize a Proxy-Authorization header.
    """
    def proxy_open(self, request, proxy, protocol):
        if getattr(request, '_artifact_explicit_proxy_set', False):
            return None
        endpoint = _proxy_endpoint(proxy)
        original = request.type
        request.set_proxy(endpoint.netloc, endpoint.scheme)
        request._artifact_explicit_proxy_set = True
        if original == endpoint.scheme or original == 'https':
            return None
        return self.parent.open(request, timeout=request.timeout)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, deadline, allow_loopback_http=False):
        self.deadline = deadline
        self.allow_loopback_http = allow_loopback_http

    def redirect_request(self, request, response, code, message, headers, new_url):
        target = _url(new_url, self.allow_loopback_http)
        if urllib.parse.urlsplit(request.full_url).scheme == 'https' and target.scheme != 'https':
            raise ArtifactFetchError('artifact_https_downgrade')
        _remaining(self.deadline)
        return super().redirect_request(request, response, code, message, headers, new_url)

    def http_error_302(self, request, response, code, message, headers):
        # The stdlib redirect implementation calls response.read() with no
        # bound. Closing the old connection avoids buffering a redirect body
        # and is sufficient for this credential-free download transport.
        try:
            locations = headers.get_all('Location', []) or headers.get_all('URI', [])
            if len(locations) != 1:
                raise ArtifactFetchError('artifact_redirect_location_required')
            target = urllib.parse.urljoin(request.full_url, locations[0])
            redirected = self.redirect_request(request, response, code, message, headers, target)
            visited = dict(getattr(request, 'redirect_dict', {}))
            if visited.get(target, 0) >= 4 or sum(visited.values()) >= 10:
                raise ArtifactFetchError('artifact_redirect_limit')
            visited[target] = visited.get(target, 0) + 1
            redirected.redirect_dict = visited
        finally:
            response.close()
        return self.parent.open(redirected, timeout=min(request.timeout, _remaining(self.deadline)))

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _header(response, name):
    values = response.headers.get_all(name, [])
    if len(values) > 1:
        raise ArtifactFetchError('artifact_duplicate_response_header')
    return values[0] if values else None


def _part(url, start, end, size, destination, timeout, deadline, stopped, allow_loopback_http,
          prefix_size=None, proxy_url=None):
    expected = end - start + 1
    request = urllib.request.Request(url, headers={
        'Range': 'bytes={}-{}'.format(start, end), 'Accept-Encoding': 'identity'})
    # No credential, cookie, or environment-derived proxy-auth channel. Native
    # networking/proxy configuration elsewhere is neither changed nor restricted.
    proxies = {} if proxy_url is None else {'http': proxy_url, 'https': proxy_url}
    opener = urllib.request.build_opener(_ExplicitProxy(proxies),
        _SafeRedirect(deadline, allow_loopback_http))
    try:
        with opener.open(request, timeout=min(timeout, _remaining(deadline, stopped))) as response:
            if response.getcode() != 206:
                raise ArtifactFetchError('artifact_part_requires_206')
            _url(response.geturl(), allow_loopback_http)
            actual_range = _header(response, 'Content-Range')
            match = re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+)', actual_range or '')
            if match is None or tuple(int(value) for value in match.groups()) != (start, end, size):
                raise ArtifactFetchError('artifact_part_range_mismatch')
            encoding = _header(response, 'Content-Encoding')
            if encoding is not None and encoding.lower().strip() != 'identity':
                raise ArtifactFetchError('artifact_encoded_range_not_supported')
            length = _header(response, 'Content-Length')
            if length is not None and (re.fullmatch(r'[0-9]+', length) is None or int(length) != expected):
                raise ArtifactFetchError('artifact_part_length_mismatch')
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL if prefix_size is None else
                     os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
            descriptor = os.open(str(destination), flags, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                if prefix_size is not None:
                    metadata = os.fstat(output.fileno())
                    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size != prefix_size):
                        raise ArtifactFetchError('artifact_resume_stage_changed')
                received = 0
                while True:
                    _remaining(deadline, stopped)
                    # read1 returns after one buffered/socket read; read(size)
                    # can hide an indefinitely trickling peer inside one call.
                    body = response.read1(CHUNK_SIZE)
                    _remaining(deadline, stopped)
                    if not body:
                        break
                    received += len(body)
                    if received > expected:
                        raise ArtifactFetchError('artifact_part_length_mismatch')
                    output.write(body)
                if received != expected:
                    raise ArtifactFetchError('artifact_part_length_mismatch')
    except ArtifactFetchError:
        raise
    except urllib.error.HTTPError as error:
        # Python 3.6 HTTPError.close() requires a real fp.
        if error.fp is not None:
            error.close()
        raise ArtifactFetchError('artifact_http_failed') from None
    except Exception as error:
        # The socket bound is clipped to the overall remaining time. A
        # timeout can therefore win the race against the check after read1
        # (or occur before open returns). urllib wraps connection timeouts in
        # URLError; only an actual timeout at the expired overall deadline is
        # a deadline failure. Unrelated failures and earlier socket timeouts
        # remain distinct transport failures, even if their messages say
        # "timed out". Never expose those potentially signed URL messages.
        cause = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(cause, (socket.timeout, TimeoutError)) and time.monotonic() >= deadline:
            raise ArtifactFetchError('artifact_fetch_deadline') from None
        # HTTP/socket exceptions can embed signed URLs: never expose their text.
        raise ArtifactFetchError('artifact_transport_failed') from None


def _output_parent(output):
    output = Path(output)
    if not output.is_absolute() or '..' in output.parts or output.name in ('', '.', '..'):
        raise ArtifactFetchError('artifact_absolute_output_required')
    for path in (output,) + tuple(output.parents):
        if path.is_symlink():
            raise ArtifactFetchError('artifact_symlink_output_not_supported')
    try:
        descriptor = os.open(str(output.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            os.close(descriptor)
            raise ArtifactFetchError('artifact_output_parent_must_be_owned')
    except OSError:
        raise ArtifactFetchError('artifact_output_parent_unavailable')
    return output, descriptor


def _already_present(parent_descriptor, name, size, sha256):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return False
    except OSError:
        raise ArtifactFetchError('artifact_existing_output_unavailable')
    with os.fdopen(descriptor, 'rb') as artifact:
        metadata = os.fstat(artifact.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size != size):
            raise ArtifactFetchError('artifact_existing_output_mismatch')
        digest = hashlib.sha256()
        for body in iter(lambda: artifact.read(CHUNK_SIZE), b''):
            digest.update(body)
        if digest.hexdigest() != sha256:
            raise ArtifactFetchError('artifact_existing_output_mismatch')
        try:
            current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (_fingerprint(metadata) != _fingerprint(os.fstat(artifact.fileno()))
                    or _fingerprint(metadata) != _fingerprint(current)):
                raise ArtifactFetchError('artifact_existing_output_changed')
        except OSError:
            raise ArtifactFetchError('artifact_existing_output_changed') from None
    return True


def _fingerprint(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
            metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


class _ResumeParts:
    """An explicit owned snapshot, copied and checked without modifying it."""
    def __init__(self, directory, lengths):
        self.descriptor = None
        self.lengths, self.metadata, self.hashes = lengths, {}, {}
        try:
            self.directory = Path(directory)
            if not self.directory.is_absolute() or '..' in self.directory.parts:
                raise ArtifactFetchError('artifact_resume_absolute_directory_required')
            for candidate in (self.directory,) + tuple(self.directory.parents):
                if candidate.is_symlink():
                    raise ArtifactFetchError('artifact_resume_symlink_not_supported')
            self.descriptor = os.open(str(self.directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            metadata = os.fstat(self.descriptor)
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700):
                raise ArtifactFetchError('artifact_resume_private_owned_directory_required')
            self.directory_metadata = _fingerprint(metadata)
            self.names = ['part-{:03d}'.format(index) for index in range(len(lengths))]
            if set(os.listdir(self.descriptor)) != set(self.names):
                raise ArtifactFetchError('artifact_resume_part_set_mismatch')
            for name, length in zip(self.names, lengths):
                metadata = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                        or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
                    raise ArtifactFetchError('artifact_resume_private_owned_part_required')
                if metadata.st_size > length:
                    raise ArtifactFetchError('artifact_resume_prefix_too_large')
                self.metadata[name] = _fingerprint(metadata)
            self.check_metadata()
        except ArtifactFetchError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ArtifactFetchError('artifact_resume_unavailable') from None

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def check_metadata(self):
        try:
            if (_fingerprint(os.fstat(self.descriptor)) != self.directory_metadata
                    or _fingerprint(self.directory.lstat()) != self.directory_metadata
                    or set(os.listdir(self.descriptor)) != set(self.names)):
                raise ArtifactFetchError('artifact_resume_checkpoint_changed')
            for name in self.names:
                if _fingerprint(os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)) != self.metadata[name]:
                    raise ArtifactFetchError('artifact_resume_checkpoint_changed')
        except ArtifactFetchError:
            raise
        except Exception:
            raise ArtifactFetchError('artifact_resume_checkpoint_changed') from None

    def _read(self, name, deadline, destination=None):
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.descriptor)
            with os.fdopen(descriptor, 'rb') as source:
                if _fingerprint(os.fstat(source.fileno())) != self.metadata[name]:
                    raise ArtifactFetchError('artifact_resume_checkpoint_changed')
                digest, copied = hashlib.sha256(), 0
                while True:
                    _remaining(deadline)
                    body = source.read(CHUNK_SIZE)
                    if not body:
                        break
                    copied += len(body)
                    if copied > self.metadata[name][5]:
                        raise ArtifactFetchError('artifact_resume_checkpoint_changed')
                    digest.update(body)
                    if destination is not None:
                        destination.write(body)
                if (copied != self.metadata[name][5]
                        or _fingerprint(os.fstat(source.fileno())) != self.metadata[name]):
                    raise ArtifactFetchError('artifact_resume_checkpoint_changed')
                return digest.hexdigest()
        except ArtifactFetchError:
            raise
        except Exception:
            raise ArtifactFetchError('artifact_resume_checkpoint_changed') from None

    def copy_to(self, stage, deadline):
        for name in self.names:
            descriptor = os.open(str(stage / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                self.hashes[name] = self._read(name, deadline, output)
        self.check_metadata()
        return [self.metadata[name][5] for name in self.names]

    def verify(self, deadline):
        self.check_metadata()
        for name in self.names:
            if self._read(name, deadline) != self.hashes[name]:
                raise ArtifactFetchError('artifact_resume_checkpoint_changed')
        self.check_metadata()


def fetch_artifact(url, size, sha256, output, workers=4, timeout=30, deadline=600,
                   allow_loopback_http=False, resume_parts_directory=None, proxy_url=None):
    """Fetch exact ranges then publish one verified 0600 file, never overwrite.

    timeout is the socket-operation bound; deadline bounds the overall transfer.
    allow_loopback_http is only for explicit offline/local HTTP fixtures. There
    are intentionally no caller-supplied headers, tokens, cookies, or auth args.
    Resume requires an explicit snapshot and the original URL/size/SHA/workers;
    a prefix has no independent trust until the complete artifact SHA matches.
    proxy_url is an explicit credential-free HTTP(S) transport for this fetch,
    never inherited from the environment. SOCKS requires a different transport;
    rejecting it here does not restrict the owner's native curl/network tools.
    """
    _url(url, allow_loopback_http, initial=True)
    _proxy_endpoint(proxy_url)
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ArtifactFetchError('artifact_positive_size_required')
    if not isinstance(sha256, str) or re.fullmatch(r'[0-9a-fA-F]{64}', sha256) is None:
        raise ArtifactFetchError('artifact_verified_sha256_required')
    sha256 = sha256.lower()
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 32:
        raise ArtifactFetchError('artifact_workers_out_of_range')
    for value in (timeout, deadline):
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value <= 0):
            raise ArtifactFetchError('artifact_positive_timeout_required')
    count = min(workers, size)
    quotient, remainder = divmod(size, count)
    lengths = [quotient + (1 if index < remainder else 0) for index in range(count)]
    output, parent_descriptor = _output_parent(output)
    resume = None
    try:
        if resume_parts_directory is not None:
            resume = _ResumeParts(resume_parts_directory, lengths)
        if _already_present(parent_descriptor, output.name, size, sha256):
            return {'output': str(output), 'size': size, 'sha256': sha256,
                    'sha256_verified': True, 'already_present': True}
        expires, stopped = time.monotonic() + deadline, threading.Event()
        with tempfile.TemporaryDirectory(prefix='.artifact-fetch-', dir=str(output.parent)) as scratch:
            stage, parts, offset = Path(scratch), [], 0
            prefixes = resume.copy_to(stage, expires) if resume is not None else [0] * count
            with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                futures = []
                for index in range(count):
                    length, prefix = lengths[index], prefixes[index]
                    part = stage / 'part-{:03d}'.format(index)
                    parts.append(part)
                    if prefix < length:
                        futures.append(pool.submit(_part, url, offset + prefix, offset + length - 1,
                            size, part, timeout, expires, stopped, allow_loopback_http,
                            prefix if resume is not None else None, proxy_url))
                    offset += length
                try:
                    for future in concurrent.futures.as_completed(futures):
                        future.result()
                except BaseException:
                    stopped.set()
                    for future in futures:
                        future.cancel()
                    raise
            complete = stage / 'complete'
            descriptor = os.open(str(complete), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            digest, assembled = hashlib.sha256(), 0
            with os.fdopen(descriptor, 'wb') as artifact:
                for part in parts:
                    with part.open('rb') as source:
                        for body in iter(lambda: source.read(CHUNK_SIZE), b''):
                            _remaining(expires)
                            artifact.write(body)
                            digest.update(body)
                            assembled += len(body)
                if assembled != size or digest.hexdigest() != sha256:
                    raise ArtifactFetchError('artifact_sha256_mismatch')
                artifact.flush()
                os.fsync(artifact.fileno())
            _remaining(expires)
            if resume is not None:
                resume.verify(expires)
                _remaining(expires)
            try:
                # Hardlink publishes the whole file atomically on the same
                # filesystem and, unlike rename/replace, never overwrites.
                os.link(str(complete), output.name, dst_dir_fd=parent_descriptor, follow_symlinks=False)
                already_present = False
                os.fsync(parent_descriptor)
            except FileExistsError:
                already_present = _already_present(parent_descriptor, output.name, size, sha256)
                if not already_present:
                    raise ArtifactFetchError('artifact_output_changed')
            return {'output': str(output), 'size': size, 'sha256': sha256,
                    'sha256_verified': True, 'already_present': already_present, 'workers': count,
                    'resumed_bytes': sum(prefixes), 'network_parts': len(futures)}
    finally:
        if resume is not None:
            resume.close()
        os.close(parent_descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='public HTTPS artifact URL, without query/auth')
    parser.add_argument('--size', required=True, type=int, help='verified complete artifact size in bytes')
    parser.add_argument('--sha256', required=True, help='complete artifact SHA256 verified from its official release')
    parser.add_argument('--output', required=True, help='absolute destination in an existing owned directory')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=30, help='socket-operation timeout in seconds')
    parser.add_argument('--deadline', type=float, default=600, help='overall deadline in seconds')
    parser.add_argument('--resume-parts-directory', help='explicit owned 0700 snapshot of every original part')
    parser.add_argument('--proxy-url', help='explicit credential-free HTTP(S) proxy for this fetch; no SOCKS/auth')
    args, unsupported = parser.parse_known_args()
    if unsupported:
        # argparse's default error embeds unknown values (potential tokens).
        print(json.dumps({'ok': False, 'error': 'artifact_unsupported_arguments'}))
        return 1
    try:
        result = fetch_artifact(args.url, args.size, args.sha256, args.output,
                                args.workers, args.timeout, args.deadline,
                                resume_parts_directory=args.resume_parts_directory, proxy_url=args.proxy_url)
    except ArtifactFetchError as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 1
    except Exception:
        print(json.dumps({'ok': False, 'error': 'artifact_fetch_failed'}))
        return 1
    result['ok'] = True
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
