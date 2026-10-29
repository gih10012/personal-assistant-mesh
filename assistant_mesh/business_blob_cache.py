"""Optional local BLOB cache seeded only from explicit owned history snapshots.

Seeds are opened with SQLite mode=ro/query_only. Only session_chunks(id,part,
body) is indexed; no Store, account configuration or other business table is
opened. Digest matches always require the complete bytes again on lookup.
"""
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .business_journal import MAX_BLOB_BYTES


def _digest(value):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in '0123456789abcdef' for character in value)):
        raise ValueError('business_blob_cache_digest_invalid')
    return value


def _regular_owned(path):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('business_blob_cache_owned_regular_file_required')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('business_blob_cache_owned_regular_file_required')
    return info


def _checked(raw, sha256, size):
    if (not isinstance(raw, bytes) or len(raw) != size or len(raw) > MAX_BLOB_BYTES
            or hashlib.sha256(raw).hexdigest() != sha256):
        raise ValueError('business_blob_cache_content_conflict')
    return raw


class BlobCache:
    def __init__(self, seed_databases=(), directory=None):
        self.directory = None if directory is None else Path(directory).absolute()
        self._index, self._seeds = {}, []
        self._stats = dict(seed_chunks_indexed=0, seed_hits=0, cache_hits=0, misses=0, stored_chunks=0)
        try:
            if self.directory is not None:
                if (self.directory.is_symlink()
                        or any(parent.is_symlink() for parent in self.directory.parents)):
                    raise ValueError('business_blob_cache_private_directory_required')
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                info = self.directory.stat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise ValueError('business_blob_cache_private_directory_required')
            for filename in seed_databases:
                self._index_seed(Path(filename).absolute())
        except BaseException:
            self.close()
            raise

    def _index_seed(self, path):
        info = _regular_owned(path)
        # Seeds are complete standalone snapshots, never live authority DBs.
        # Immutable read-only mode also prevents SQLite creating WAL sidecars.
        if Path(str(path) + '-wal').exists():
            raise ValueError('business_blob_cache_standalone_seed_required')
        db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, isolation_level=None)
        self._seeds.append(db)
        db.execute('PRAGMA query_only=ON')
        columns = db.execute('PRAGMA table_info(session_chunks)').fetchall()
        if (not {'id', 'part', 'body'} <= {row[1] for row in columns}
                or [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]] != ['id', 'part']):
            raise ValueError('business_blob_cache_seed_schema_invalid')
        identity = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        for identifier, part, raw in db.execute('SELECT id,part,body FROM session_chunks'):
            # Non-BLOB storage types are transported inline, never referenced.
            if not isinstance(raw, bytes):
                continue
            if len(raw) > MAX_BLOB_BYTES:
                raise ValueError('business_blob_cache_seed_chunk_exceeds_budget')
            sha256 = hashlib.sha256(raw).hexdigest()
            self._index.setdefault(sha256, (path, identity, db, identifier, part, len(raw)))
            self._stats['seed_chunks_indexed'] += 1

    def get(self, sha256, size):
        _digest(sha256)
        if type(size) is not int or not 0 <= size <= MAX_BLOB_BYTES:
            raise ValueError('business_blob_cache_size_invalid')
        if self.directory is not None:
            path = self.directory / sha256
            if path.exists() or path.is_symlink():
                info = _regular_owned(path)
                if stat.S_IMODE(info.st_mode) & 0o077 or info.st_size != size:
                    raise ValueError('business_blob_cache_content_conflict')
                with path.open('rb') as stream:
                    raw = stream.read(MAX_BLOB_BYTES + 1)
                _checked(raw, sha256, size)
                self._stats['cache_hits'] += 1
                return raw
        seed = self._index.get(sha256)
        if seed is not None:
            path, identity, db, identifier, part, indexed_size = seed
            try:
                info = _regular_owned(path)
                if identity != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) or indexed_size != size:
                    raise ValueError('business_blob_cache_seed_changed')
                row = db.execute('SELECT body FROM session_chunks WHERE id=? AND part=?',
                                 (identifier, part)).fetchone()
                if row is None:
                    raise ValueError('business_blob_cache_seed_changed')
                raw = _checked(row[0], sha256, size)
            except (OSError, ValueError, sqlite3.Error):
                # A changed/disconnected seed is a miss; source fetch remains
                # authenticated and bounded to the admitted snapshot page.
                raw = None
            if raw is not None:
                self._stats['seed_hits'] += 1
                return raw
        self._stats['misses'] += 1
        return None

    def put(self, sha256, raw):
        _digest(sha256)
        _checked(raw, sha256, len(raw))
        if self.directory is None:
            return
        target = self.directory / sha256
        descriptor, temporary = tempfile.mkstemp(prefix='.blob-', dir=str(self.directory))
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(target))
            self._stats['stored_chunks'] += 1
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def status(self):
        return dict(self._stats)

    def close(self):
        for db in self._seeds:
            db.close()
        self._seeds = []
        self._index = {}

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
