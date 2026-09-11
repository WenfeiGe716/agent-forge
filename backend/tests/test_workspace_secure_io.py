"""Defensive IO regressions using only pytest-owned temporary files."""
import asyncio
import os
from pathlib import Path

import pytest

from forge import tools, workspace
from forge.schema import DEFAULT_RUNTIME


def call(root, operation, arguments):
    return asyncio.run(tools.invoke(operation, arguments, [operation], root, 'a' * 32, 'task', 'io-test', DEFAULT_RUNTIME, 'unused'))


def test_read_callsite_does_not_reopen_a_validated_path_by_name(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'; root.mkdir()
    file = root / 'file.txt'; file.write_text('inside')
    other = tmp_path / 'unrelated.txt'; other.write_text('unrelated fixture')
    original = Path.read_bytes

    def replaced_read(path):
        if path == file:
            file.unlink(); file.symlink_to(other)
        return original(path)

    monkeypatch.setattr(Path, 'read_bytes', replaced_read)
    result = call(root, 'read_file', {'path': 'file.txt'})
    assert result['content'] == 'inside', 'validated reads must retain the verified file descriptor'


def test_write_callsite_does_not_reopen_a_parent_after_path_based_mkdir(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'; root.mkdir()
    other = tmp_path / 'unrelated'; other.mkdir()
    parent = root / 'src'
    original = Path.mkdir

    def replaced_mkdir(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == parent:
            parent.rename(root / 'original-src'); parent.symlink_to(other, target_is_directory=True)
        return result

    monkeypatch.setattr(Path, 'mkdir', replaced_mkdir)
    result = call(root, 'write_file', {'path': 'src/output.txt', 'content': 'approved'})
    assert result == {'written': 'src/output.txt', 'bytes': 8}
    assert not (other / 'output.txt').exists(), 'parent directory changes must not redirect writes'
    assert (root / 'src' / 'output.txt').read_text() == 'approved'


def test_normal_bounded_read_atomic_write_and_delete(tmp_path):
    workspace.write_workspace_file(tmp_path, 'nested/src/file.txt', b'first', 100)
    file = tmp_path / 'nested/src/file.txt'; file.chmod(0o750)
    workspace.write_workspace_file(tmp_path, 'nested/src/file.txt', b'updated', 100)
    assert workspace.read_workspace_file(tmp_path, 'nested/src/file.txt', 100) == b'updated'
    assert file.stat().st_mode & 0o777 == 0o750
    assert not list(file.parent.glob('.forge-write-*'))
    assert workspace.delete_workspace_file(tmp_path, 'nested/src/file.txt') is True
    assert workspace.delete_workspace_file(tmp_path, 'nested/src/file.txt') is False
    with pytest.raises(ValueError): workspace.read_workspace_file(tmp_path, 'nested/src/file.txt', 100)


@pytest.mark.parametrize('relative', ['', '.', '../file', '/absolute', 'a/../../file', 'a\\b', '.git/config', '.env', 'keys/server.pem', 'private-credentials.json', 'a\x00b'])
def test_original_lexical_and_secret_path_rules_apply_to_all_io(tmp_path, relative):
    with pytest.raises(ValueError): workspace.read_workspace_file(tmp_path, relative, 100)
    with pytest.raises(ValueError): workspace.write_workspace_file(tmp_path, relative, b'x', 100)
    with pytest.raises(ValueError): workspace.delete_workspace_file(tmp_path, relative)


@pytest.mark.parametrize('operation', ['read', 'write', 'delete'])
@pytest.mark.parametrize('kind', ['leaf_symlink', 'parent_symlink', 'hardlink'])
def test_links_are_rejected_without_touching_the_other_fixture(tmp_path, operation, kind):
    root = tmp_path / 'workspace'; root.mkdir()
    outside = tmp_path / 'unrelated'; outside.mkdir()
    other = outside / 'file.txt'; other.write_bytes(b'unrelated')
    relative = 'file.txt'
    if kind == 'leaf_symlink': (root / relative).symlink_to(other)
    elif kind == 'parent_symlink':
        (root / 'parent').symlink_to(outside, target_is_directory=True); relative = 'parent/file.txt'
    else: os.link(other, root / relative)
    with pytest.raises(ValueError):
        if operation == 'read': workspace.read_workspace_file(root, relative, 100)
        elif operation == 'write': workspace.write_workspace_file(root, relative, b'new', 100)
        else: workspace.delete_workspace_file(root, relative)
    assert other.read_bytes() == b'unrelated'


def test_leaf_replacement_before_open_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'; root.mkdir()
    file = root / 'file.txt'; file.write_text('inside')
    other = tmp_path / 'unrelated.txt'; other.write_text('unrelated')
    original = os.open
    changed = False

    def replace_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal changed
        if path == 'file.txt' and dir_fd is not None and not changed:
            changed = True; file.unlink(); file.symlink_to(other)
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'open', replace_then_open)
    with pytest.raises(ValueError): workspace.read_workspace_file(root, 'file.txt', 100)
    assert changed and other.read_text() == 'unrelated'


@pytest.mark.parametrize('operation', ['read', 'write', 'delete'])
def test_parent_name_replacement_is_rejected_using_open_directory_identity(tmp_path, monkeypatch, operation):
    root = tmp_path / 'workspace'; root.mkdir()
    parent = root / 'parent'; parent.mkdir(); (parent / 'file.txt').write_bytes(b'inside')
    other = tmp_path / 'unrelated'; other.mkdir(); (other / 'file.txt').write_bytes(b'unrelated')
    original = os.stat
    changed = False

    def replace_after_stat(path, *args, **kwargs):
        nonlocal changed
        result = original(path, *args, **kwargs)
        if path == 'file.txt' and kwargs.get('dir_fd') is not None and not changed:
            changed = True; parent.rename(root / 'original-parent'); parent.symlink_to(other, target_is_directory=True)
        return result

    monkeypatch.setattr(os, 'stat', replace_after_stat)
    with pytest.raises(ValueError):
        if operation == 'read': workspace.read_workspace_file(root, 'parent/file.txt', 100)
        elif operation == 'write': workspace.write_workspace_file(root, 'parent/file.txt', b'new', 100)
        else: workspace.delete_workspace_file(root, 'parent/file.txt')
    assert changed
    assert (other / 'file.txt').read_bytes() == b'unrelated'
    assert (root / 'original-parent/file.txt').read_bytes() == b'inside'
    assert not list((root / 'original-parent').glob('.forge-write-*'))


def test_oversized_and_nonregular_files_are_rejected_without_leaving_temporary_files(tmp_path):
    file = tmp_path / 'big.txt'; file.write_bytes(b'12345')
    with pytest.raises(ValueError): workspace.read_workspace_file(tmp_path, 'big.txt', 4)
    with pytest.raises(ValueError): workspace.write_workspace_file(tmp_path, 'big.txt', b'12345', 4)
    (tmp_path / 'folder').mkdir()
    for operation in ('read', 'write', 'delete'):
        with pytest.raises(ValueError):
            if operation == 'read': workspace.read_workspace_file(tmp_path, 'folder', 100)
            elif operation == 'write': workspace.write_workspace_file(tmp_path, 'folder', b'new', 100)
            else: workspace.delete_workspace_file(tmp_path, 'folder')
    assert file.read_bytes() == b'12345'
    assert not list(tmp_path.glob('.forge-write-*'))


def test_atomic_replace_failure_cleans_only_the_owned_temporary_file(tmp_path, monkeypatch):
    file = tmp_path / 'file.txt'; file.write_bytes(b'original')
    def fail(*args, **kwargs): raise OSError('fixture replace failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(ValueError): workspace.write_workspace_file(tmp_path, 'file.txt', b'changed', 100)
    assert file.read_bytes() == b'original'
    assert not list(tmp_path.glob('.forge-write-*'))


def test_file_growth_during_read_still_obeys_the_byte_limit(tmp_path, monkeypatch):
    file = tmp_path / 'file.txt'; file.write_bytes(b'1234')
    original = os.read
    grew = False
    def grow(fd, size):
        nonlocal grew
        if not grew:
            grew = True
            with file.open('ab') as stream: stream.write(b'56789')
        return original(fd, size)
    monkeypatch.setattr(os, 'read', grow)
    with pytest.raises(ValueError): workspace.read_workspace_file(tmp_path, 'file.txt', 4)


def test_tool_json_shape_is_unchanged_and_delete_missing_is_not_reported_as_success(tmp_path):
    call(tmp_path, 'write_file', {'path': 'a.txt', 'content': 'hello'})
    assert call(tmp_path, 'read_file', {'path': 'a.txt'})['content'] == 'hello'
    assert call(tmp_path, 'delete_file', {'path': 'a.txt'}) == {'deleted': 'a.txt'}
    with pytest.raises(ValueError): call(tmp_path, 'delete_file', {'path': 'a.txt'})
