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


def _part(url, start, end, size, destination, timeout, deadline, stopped, allow_loopback_http):
    expected = end - start + 1
    request = urllib.request.Request(url, headers={
        'Range': 'bytes={}-{}'.format(start, end), 'Accept-Encoding': 'identity'})
    # No credential, cookie, or environment-derived proxy-auth channel. Native
    # networking/proxy configuration elsewhere is neither changed nor restricted.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
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
            descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
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
    except Exception:
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
    return True


def fetch_artifact(url, size, sha256, output, workers=4, timeout=30, deadline=600,
                   allow_loopback_http=False):
    """Fetch exact ranges then publish one verified 0600 file, never overwrite.

    timeout is the socket-operation bound; deadline bounds the overall transfer.
    allow_loopback_http is only for explicit offline/local HTTP fixtures. There
    are intentionally no caller-supplied headers, tokens, cookies, or auth args.
    """
    _url(url, allow_loopback_http, initial=True)
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
    output, parent_descriptor = _output_parent(output)
    try:
        if _already_present(parent_descriptor, output.name, size, sha256):
            return {'output': str(output), 'size': size, 'sha256': sha256,
                    'sha256_verified': True, 'already_present': True}
        expires, stopped = time.monotonic() + deadline, threading.Event()
        count = min(workers, size)
        with tempfile.TemporaryDirectory(prefix='.artifact-fetch-', dir=str(output.parent)) as scratch:
            stage, parts, offset = Path(scratch), [], 0
            quotient, remainder = divmod(size, count)
            with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                futures = []
                for index in range(count):
                    length = quotient + (1 if index < remainder else 0)
                    part = stage / 'part-{:03d}'.format(index)
                    parts.append(part)
                    futures.append(pool.submit(_part, url, offset, offset + length - 1, size, part,
                        timeout, expires, stopped, allow_loopback_http))
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
            try:
                # Hardlink publishes the whole file atomically on the same
                # filesystem and, unlike rename/replace, never overwrites.
                os.link(str(complete), output.name, dst_dir_fd=parent_descriptor, follow_symlinks=False)
                already_present = False
                os.fsync(parent_descriptor)
            except FileExistsError:
                already_present = _already_present(parent_descriptor, output.name, size, sha256)
            return {'output': str(output), 'size': size, 'sha256': sha256,
                    'sha256_verified': True, 'already_present': already_present, 'workers': count}
    finally:
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
    args, unsupported = parser.parse_known_args()
    if unsupported:
        # argparse's default error embeds unknown values (potential tokens).
        print(json.dumps({'ok': False, 'error': 'artifact_unsupported_arguments'}))
        return 1
    try:
        result = fetch_artifact(args.url, args.size, args.sha256, args.output,
                                args.workers, args.timeout, args.deadline)
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
