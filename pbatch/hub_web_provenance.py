"""Validate frontend build inputs; digests are not signatures or acceptance."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

INPUT_FILES = ('package.json', 'bun.lock', 'iris-source.json', 'index.html',
               'tsconfig.json', 'svelte.config.js', 'vite.config.ts')
INPUT_DIRS = ('src', 'scripts', 'tests')
MAX_FILES = 128
MAX_FILE_BYTES = 262144
MAX_TOTAL_BYTES = 2097152


def validate_build(build):
    if (not isinstance(build, dict) or set(build) != {'bun', 'sources'}
            or not isinstance(build['bun'], str)
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', build['bun'])):
        raise ValueError('invalid frontend build toolchain')
    sources = build['sources']
    if (not isinstance(sources, dict) or not 1 <= len(sources) <= MAX_FILES
            or not set(INPUT_FILES) <= sources.keys()):
        raise ValueError('missing or excessive frontend build inputs')
    total = 0
    for name, row in sources.items():
        _validate_row(name, row)
        total += row['bytes']
    if total > MAX_TOTAL_BYTES:
        raise ValueError('frontend build inputs exceed total byte budget')


def _validate_row(name, row):
    if not isinstance(name, str):
        raise ValueError('invalid frontend build input path')
    parts = name.split('/')
    if (name not in INPUT_FILES and (len(parts) < 2 or parts[0] not in INPUT_DIRS
            or any(part in {'', '.', '..'} or not re.fullmatch(r'[A-Za-z0-9_.-]+', part)
                   for part in parts))):
        raise ValueError('invalid frontend build input path')
    if (not isinstance(row, dict) or set(row) != {'bytes', 'sha256'}
            or type(row['bytes']) is not int or not 0 <= row['bytes'] <= MAX_FILE_BYTES
            or not isinstance(row['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])):
        raise ValueError('invalid frontend build input digest')


def _read_source(path):
    if path.is_symlink():
        raise ValueError('frontend source links are not build inputs')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError('frontend source type or size exceeds contract')
        raw = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
        if (len(raw) != before.st_size or before.st_size != after.st_size
                or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)
                or not os.path.samestat(after, path.stat(follow_symlinks=False))):
            raise ValueError('frontend source changed while reading')
        return raw


def _collect(root, directory, result, visited, depth=0):
    if directory.is_symlink() or depth > 8:
        raise ValueError('frontend source directory link or excessive depth')
    with os.scandir(directory) as entries:
        for entry in entries:
            visited[0] += 1
            if visited[0] > 256:
                raise ValueError('frontend source directory entry budget exceeded')
            path = Path(entry.path)
            if entry.is_symlink():
                raise ValueError('frontend source links are not build inputs')
            if entry.is_dir(follow_symlinks=False):
                _collect(root, path, result, visited, depth + 1)
            else:
                _record(root, path, result)


def _record(root, path, result):
    if len(result) >= MAX_FILES:
        raise ValueError('frontend build input count exceeded')
    raw = _read_source(path)
    result[path.relative_to(root).as_posix()] = {
        'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    if sum(row['bytes'] for row in result.values()) > MAX_TOTAL_BYTES:
        raise ValueError('frontend build inputs exceed total byte budget')


def source_files(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError('frontend source root must not be a link')
    result = {}
    for name in INPUT_FILES:
        _record(root, root / name, result)
    visited = [0]
    for name in INPUT_DIRS:
        _collect(root, root / name, result, visited)
    return dict(sorted(result.items()))


def check_sources(root, manifest):
    """Offline drift check only; rebuild comparison is a separate Bun gate."""
    validate_build(manifest.get('build'))
    actual = source_files(root)
    if actual != manifest['build']['sources']:
        raise ValueError('frontend sources changed; rebuild and verify before shipping')
    package = json.loads(_read_source(Path(root) / 'package.json'))
    iris = json.loads(_read_source(Path(root) / 'iris-source.json'))
    if package.get('packageManager') != 'bun@' + manifest['build']['bun'] or iris != manifest['iris']:
        raise ValueError('frontend toolchain or Iris provenance does not match')
