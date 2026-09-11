"""Defensive export regressions using only pytest-owned temporary repositories."""
import os
import stat
from dataclasses import replace

import pytest

from forge import workspace as ws


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'task'
    root.mkdir()
    (root / 'keep.txt').write_text('baseline\n')
    (root / 'delete.txt').write_text('delete me\n')
    ws.git(root, 'init', '-b', 'main')
    ws.git(root, 'add', '-A')
    ws.git(root, 'commit', '-m', 'baseline')
    return root, ws.git(root, 'rev-parse', 'HEAD').stdout.decode().strip()


def test_export_git_only_opens_private_frozen_staging(repository, monkeypatch):
    root, base = repository
    (root / 'keep.txt').write_text('before snapshot\n')
    source_index = (root / '.git/index').read_bytes()
    actual_git = ws.git
    seen = []

    def observe_git(cwd, *args, **kwargs):
        assert cwd != root, 'Host Git must never reopen the live task tree during export'
        assert cwd.parent == root.parent and not cwd.is_relative_to(root)
        assert stat.S_IMODE(cwd.stat().st_mode) == 0o700
        seen.append(cwd)
        # An ordinary edit after the snapshot has completed must not change export.
        (root / 'keep.txt').write_text('later task edit\n')
        return actual_git(cwd, *args, **kwargs)

    monkeypatch.setattr(ws, 'git', observe_git)
    patch = ws.patch_for(root, base)
    assert b'+before snapshot' in patch and b'later task edit' not in patch
    assert seen and all(not path.exists() for path in seen)
    assert (root / '.git/index').read_bytes() == source_index


def test_snapshot_preserves_add_delete_binary_executable_and_base(repository, tmp_path):
    root, base = repository
    destination = tmp_path / 'integration'
    ws.copy_repo(root, destination)
    (root / 'delete.txt').unlink()
    (root / 'keep.txt').write_text('modified\n')
    (root / 'nested').mkdir()
    (root / 'nested/new.txt').write_text('new\n')
    (root / 'binary.dat').write_bytes(bytes(range(256)))
    (root / 'runner.sh').write_text('#!/bin/sh\nexit 0\n')
    (root / 'runner.sh').chmod(0o755)
    (root / '.env').write_text('fixture must not export')
    patch = ws.patch_for(root, base)
    ws.git(destination, 'apply', '--check', '--index', input_bytes=patch)
    ws.git(destination, 'apply', '--index', input_bytes=patch)
    assert (destination / 'keep.txt').read_text() == 'modified\n'
    assert not (destination / 'delete.txt').exists()
    assert (destination / 'nested/new.txt').read_text() == 'new\n'
    assert (destination / 'binary.dat').read_bytes() == bytes(range(256))
    assert (destination / 'runner.sh').stat().st_mode & 0o111
    assert not (destination / '.env').exists()
    assert not list(root.parent.glob('.forge-export-*'))


def test_empty_export_is_empty_and_does_not_change_source_index(repository):
    root, base = repository
    index = (root / '.git/index').read_bytes()
    assert ws.patch_for(root, base) == b''
    assert (root / '.git/index').read_bytes() == index


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'parent_symlink', 'git_symlink'])
def test_snapshot_rejects_unsafe_entries(repository, tmp_path, kind):
    root, base = repository
    fixture = tmp_path / 'other-fixture'
    fixture.mkdir()
    (fixture / 'other.txt').write_text('unrelated fixture')
    if kind == 'symlink':
        (root / 'link.txt').symlink_to(fixture / 'other.txt')
    elif kind == 'hardlink':
        os.link(fixture / 'other.txt', root / 'link.txt')
    elif kind == 'parent_symlink':
        (root / 'linked').symlink_to(fixture, target_is_directory=True)
    else:
        (root / '.git/config').unlink()
        (root / '.git/config').symlink_to(fixture / 'other.txt')
    with pytest.raises(ValueError):
        ws.patch_for(root, base)
    assert (fixture / 'other.txt').read_text() == 'unrelated fixture'
    assert not list(root.parent.glob('.forge-export-*'))


def test_export_size_limit_fails_closed_and_cleans_staging(repository, monkeypatch):
    root, base = repository
    monkeypatch.setattr(ws, 'settings', replace(ws.settings, max_project_mb=0))
    with pytest.raises(ValueError, match='上限|大小'):
        ws.patch_for(root, base)
    assert not list(root.parent.glob('.forge-export-*'))


def test_export_git_failure_cleans_only_owned_staging(repository, monkeypatch):
    root, base = repository
    marker = root.parent / '.forge-export-unrelated'
    marker.mkdir()
    (marker / 'keep').write_text('keep this fixture')

    def failure(*args, **kwargs):
        raise ValueError('fixture git failure')

    monkeypatch.setattr(ws, 'git', failure)
    with pytest.raises(ValueError, match='fixture git failure'):
        ws.patch_for(root, base)
    assert (marker / 'keep').read_text() == 'keep this fixture'
    assert list(root.parent.glob('.forge-export-*')) == [marker]


def test_export_file_count_limit_cleans_staging(repository, monkeypatch):
    root, base = repository
    monkeypatch.setattr(ws, 'MAX_SNAPSHOT_FILES', 1)
    with pytest.raises(ValueError, match='上限'):
        ws.patch_for(root, base)
    assert not list(root.parent.glob('.forge-export-*'))


def test_snapshot_rejects_file_edited_during_capture(repository, monkeypatch):
    root, base = repository
    actual_read = ws._read_regular_at
    changed = False

    def read_after_edit(parent_fd, name, info, maximum, verify):
        nonlocal changed
        if name == 'keep.txt' and not changed:
            changed = True
            (root / 'keep.txt').write_text('a concurrent ordinary edit\n')
        return actual_read(parent_fd, name, info, maximum, verify)

    monkeypatch.setattr(ws, '_read_regular_at', read_after_edit)
    with pytest.raises(ValueError, match='变化'):
        ws.patch_for(root, base)
    assert changed and not list(root.parent.glob('.forge-export-*'))


def test_deleting_every_content_file_still_exports_valid_patch(repository, tmp_path):
    root, base = repository
    destination = tmp_path / 'integration'
    ws.copy_repo(root, destination)
    (root / 'keep.txt').unlink()
    (root / 'delete.txt').unlink()
    patch = ws.patch_for(root, base)
    ws.git(destination, 'apply', '--index', input_bytes=patch)
    assert ws.list_files(destination) == []


def test_safe_listing_is_sorted_bounded_and_excludes_secrets(tmp_path):
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested/z.txt').write_text('z')
    (tmp_path / 'a.txt').write_text('a')
    (tmp_path / '.env').write_text('test fixture, never enumerated')
    assert ws.list_files(tmp_path) == ['a.txt', 'nested/z.txt']
    assert ws.list_files(tmp_path, limit=1) == ['a.txt']


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'parent_symlink'])
def test_safe_listing_rejects_links(tmp_path, kind):
    root = tmp_path / 'root'
    root.mkdir()
    fixture = tmp_path / 'other-fixture'
    fixture.mkdir()
    (fixture / 'data.txt').write_text('fixture')
    if kind == 'hardlink':
        os.link(fixture / 'data.txt', root / 'linked')
    else:
        (root / 'linked').symlink_to(fixture if kind == 'parent_symlink' else fixture / 'data.txt')
    with pytest.raises(ValueError):
        ws.list_files(root)
