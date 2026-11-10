import base64
import hashlib
import os

import pytest

from pbatch.hub_file_capture import capture_files, root_identity
from pbatch.hub_gateway_config import HubGatewayConfig, normalize_config


def test_explicit_paths_default_off_and_are_normalized(tmp_path):
    assert normalize_config(HubGatewayConfig('http://127.0.0.1', cwd=str(tmp_path))).share_files == ()
    config = HubGatewayConfig('http://127.0.0.1', cwd=str(tmp_path), share_files=['out.txt'])
    assert normalize_config(config).share_files == ('out.txt',)


@pytest.mark.parametrize('paths', [['.env'], ['../out'], ['a', 'a/b'], ['a', 'A'], ['a'] * 9, 'a'])
def test_invalid_allowlist_fails_before_execution(paths):
    with pytest.raises(ValueError):
        normalize_config(HubGatewayConfig('http://127.0.0.1', share_files=paths))


def test_capture_only_exact_paths_and_preserves_binary_bytes(tmp_path):
    raw = b'\xff\x00document\r\n'
    (tmp_path / 'out.bin').write_bytes(raw)
    (tmp_path / 'ignored.txt').write_text('unrequested')
    result = capture_files(str(tmp_path), ['out.bin'], root_identity(str(tmp_path)))
    assert result['unavailable'] == []
    assert len(result['files']) == 1
    row = result['files'][0]
    assert base64.b64decode(row['content_b64']) == raw
    assert row['sha256'] == hashlib.sha256(raw).hexdigest()
    assert row['size_bytes'] == len(raw)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory', 'missing', 'large'])
def test_unsafe_files_are_unavailable(tmp_path, kind):
    target = tmp_path / 'out'
    if kind == 'symlink':
        target.symlink_to('/etc/passwd')
    elif kind == 'hardlink':
        (tmp_path / 'source').write_text('private')
        os.link(tmp_path / 'source', target)
    elif kind == 'fifo':
        os.mkfifo(target)
    elif kind == 'directory':
        target.mkdir()
    elif kind == 'large':
        target.write_bytes(b'x' * 65537)
    result = capture_files(str(tmp_path), ['out'], root_identity(str(tmp_path)))
    assert result['files'] == []
    assert result['unavailable'][0]['path'] == 'out'


def test_replaced_workspace_root_is_never_read(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    identity = root_identity(str(workspace))
    workspace.rename(tmp_path / 'old')
    workspace.mkdir()
    (workspace / 'out.txt').write_text('replacement must not be shared')
    result = capture_files(str(workspace), ['out.txt'], identity)
    assert result == {'files': [], 'unavailable': [{'path': 'out.txt', 'code': 'changed'}]}


def test_known_secret_rejects_entire_file(tmp_path):
    (tmp_path / 'out.txt').write_text('-----BEGIN PRIVATE KEY-----\nsecret')
    result = capture_files(str(tmp_path), ['out.txt'], root_identity(str(tmp_path)))
    assert result == {'files': [], 'unavailable': [{'path': 'out.txt', 'code': 'secret_detected'}]}


def test_total_capture_budget(tmp_path):
    paths = [f'out-{i}.bin' for i in range(5)]
    for path in paths:
        (tmp_path / path).write_bytes(b'x' * 65536)
    result = capture_files(str(tmp_path), paths, root_identity(str(tmp_path)))
    assert sum(row['size_bytes'] for row in result['files']) == 262144
    assert result['unavailable'] == [{'path': paths[-1], 'code': 'capacity_exceeded'}]
