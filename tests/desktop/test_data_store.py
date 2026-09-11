import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import pytest
from cryptography.fernet import Fernet

spec = importlib.util.spec_from_file_location('desktop_data_store', Path(__file__).resolve().parents[2] / 'desktop/data_store.py')
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


def legacy(tmp_path, status='completed'):
    root = tmp_path / 'source'
    local = root / '.local'
    data = local / 'data'
    data.mkdir(parents=True)
    (data / 'artifact.md').write_text('original evidence')
    (root / 'imports').mkdir()
    (root / 'imports' / 'code.py').write_text('print(1)')
    keys = {'ENCRYPTION_KEY': Fernet.generate_key().decode(), 'ADMIN_PASSWORD': 'legacy-test-password', 'SANDBOX_TOKEN': 'legacy-test-broker'}
    values = {**keys, 'DATABASE_URL': 'sqlite:///' + str(local / 'forge.db'), 'DATA_ROOT': str(data), 'IMPORT_ROOT': str(root / 'imports')}
    (local / 'config.json').write_text(json.dumps(values))
    with sqlite3.connect(local / 'forge.db') as db:
        db.executescript('CREATE TABLE runs (id TEXT, status TEXT); CREATE TABLE sessions (token TEXT); CREATE TABLE providers(id TEXT); CREATE TABLE provider_revisions(encrypted_key TEXT);')
        db.execute('INSERT INTO runs VALUES (?,?)', ('run-1', status))
        db.execute('INSERT INTO sessions VALUES (?)', ('browser-session',))
        db.execute('INSERT INTO providers VALUES (?)', ('provider-1',))
        db.execute('INSERT INTO provider_revisions VALUES (?)', (Fernet(keys['ENCRYPTION_KEY']).encrypt(b'fixture-api-secret').decode(),))
    return root, values


def test_new_workspace_stages_without_plaintext_keys(tmp_path):
    dest = tmp_path / 'desktop' / 'workspace'
    result = store.prepare(str(dest))
    stage = Path(result['staging_dir'])
    assert not dest.exists()
    assert result['migrated'] is False
    assert stage.stat().st_mode & 0o777 == 0o700
    config = (stage / 'config.json').read_text()
    for value in result['secrets'].values():
        assert value not in config
    assert not (stage / 'secrets.enc').exists()


def test_migrate_preserves_keys_data_and_source_but_drops_sessions_in_copy(tmp_path):
    root, values = legacy(tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result = store.prepare(str(tmp_path / 'desktop/workspace'), str(root))
    stage = Path(result['staging_dir'])
    assert result['secrets']['ENCRYPTION_KEY'] == values['ENCRYPTION_KEY']
    assert (stage / 'data/artifact.md').read_text() == 'original evidence'
    assert result['counts']['providers'] == 1
    with sqlite3.connect(stage / 'forge.db') as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
        encrypted = db.execute('SELECT encrypted_key FROM provider_revisions').fetchone()[0]
        assert Fernet(result['secrets']['ENCRYPTION_KEY']).decrypt(encrypted.encode()) == b'fixture-api-secret'
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('status', ['running', 'queued', 'paused', 'awaiting_approval', 'awaiting_decision'])
def test_active_work_blocks_copy_and_cleans_partial_stage(tmp_path, status):
    root, _ = legacy(tmp_path, status)
    with pytest.raises(ValueError, match='未结束任务'):
        store.prepare(str(tmp_path / 'desktop/workspace'), str(root))
    assert list((tmp_path / 'desktop').iterdir()) == []


def test_existing_destination_never_overwritten(tmp_path):
    dest = tmp_path / 'workspace'
    dest.mkdir()
    (dest / 'keep').write_text('mine')
    with pytest.raises(ValueError, match='不会覆盖'):
        store.prepare(str(dest))
    assert (dest / 'keep').read_text() == 'mine'


def test_overlapping_destination_is_rejected_before_any_source_mutation(tmp_path):
    root, _ = legacy(tmp_path)
    before = {p.relative_to(root) for p in root.rglob('*')}
    with pytest.raises(ValueError, match='相互包含'):
        store.prepare(str(root / 'new-parent/workspace'), str(root))
    assert before == {p.relative_to(root) for p in root.rglob('*')}


def test_wal_migration_never_creates_or_changes_source_sidecars(tmp_path):
    root, _ = legacy(tmp_path)
    database = root / '.local/forge.db'
    connection = sqlite3.connect(database)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.close()
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    store.prepare(str(tmp_path / 'desktop/workspace'), str(root))
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_committed_wal_data_is_preserved_without_source_writes(tmp_path):
    root, _ = legacy(tmp_path)
    database = root / '.local/forge.db'
    connection = sqlite3.connect(database)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('INSERT INTO providers VALUES (?)', ('committed-in-wal',))
        connection.commit()
        assert Path(str(database) + '-wal').stat().st_size > 0
        before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        result = store.prepare(str(tmp_path / 'desktop/workspace'), str(root))
        assert result['counts']['providers'] == 2
        assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        assert not list(Path(result['staging_dir']).glob('.database-*'))
    finally:
        connection.close()


def test_database_write_during_tree_copy_rejects_migration(tmp_path, monkeypatch):
    root, _ = legacy(tmp_path)
    database = root / '.local/forge.db'
    actual_copy = store.copy_private_tree
    changed = False

    def copy_then_write(source, destination):
        nonlocal changed
        actual_copy(source, destination)
        if not changed:
            changed = True
            connection = sqlite3.connect(database)
            try:
                connection.execute('INSERT INTO providers VALUES (?)', ('concurrent-change',))
                connection.commit()
            finally:
                connection.close()

    monkeypatch.setattr(store, 'copy_private_tree', copy_then_write)
    with pytest.raises(ValueError, match='发生写入'):
        store.prepare(str(tmp_path / 'desktop/workspace'), str(root))
    assert changed and list((tmp_path / 'desktop').iterdir()) == []


def test_config_directory_cannot_redirect_outside_selected_source(tmp_path):
    root, _ = legacy(tmp_path)
    original = root / '.local'
    outside = tmp_path / 'outside-local'
    original.rename(outside)
    original.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='之外'):
        store.prepare(str(tmp_path / 'desktop/workspace'), str(root))
    assert list((tmp_path / 'desktop').iterdir()) == []


def test_copy_roots_cannot_include_plaintext_legacy_config(tmp_path):
    root, values = legacy(tmp_path)
    values['DATA_ROOT'] = str(root / '.local')
    (root / '.local/config.json').write_text(json.dumps(values))
    with pytest.raises(ValueError, match='原凭据配置或数据库'):
        store.prepare(str(tmp_path / 'desktop/workspace'), str(root))
    assert list((tmp_path / 'desktop').iterdir()) == []


def test_mismatched_legacy_encryption_key_never_silently_rotates(tmp_path):
    root, values = legacy(tmp_path)
    values['ENCRYPTION_KEY'] = Fernet.generate_key().decode()
    (root / '.local/config.json').write_text(json.dumps(values))
    with pytest.raises(ValueError, match='无法解密供应商凭据'):
        store.prepare(str(tmp_path / 'desktop/workspace'), str(root))
    assert list((tmp_path / 'desktop').iterdir()) == []


def test_no_missing_key_fallback(tmp_path):
    root, values = legacy(tmp_path)
    del values['ENCRYPTION_KEY']
    (root / '.local/config.json').write_text(json.dumps(values))
    with pytest.raises(ValueError, match='缺少凭据密钥'):
        store.prepare(str(tmp_path / 'workspace'), str(root))


def test_symlink_and_external_data_not_copied(tmp_path):
    root, values = legacy(tmp_path)
    outside = tmp_path / 'secret'
    outside.write_text('not authorized')
    (root / '.local/data/link').symlink_to(outside)
    with pytest.raises(ValueError, match='链接'):
        store.prepare(str(tmp_path / 'desktop/workspace'), str(root))
    (root / '.local/data/link').unlink()
    values['DATA_ROOT'] = str(tmp_path)
    (root / '.local/config.json').write_text(json.dumps(values))
    with pytest.raises(ValueError, match='之外'):
        store.prepare(str(tmp_path / 'desktop/workspace'), str(root))


def test_environment_forces_owned_local_data_and_modes(tmp_path):
    result = store.prepare(str(tmp_path / 'workspace'))
    stage = Path(result['staging_dir'])
    with pytest.raises(ValueError, match='钥匙串'):
        store.serve_environment({'data_dir': str(stage)})
    (stage / 'secrets.enc').write_bytes(b'sealed-by-main')
    command = {'data_dir': str(stage), 'secrets': result['secrets'], 'port': 49152,
               'desktop_token': 'd'*40, 'control_token': 'c'*40}
    env = store.serve_environment(command)
    assert env['FORGE_LOCAL_EXECUTION'] == 'true'
    assert env['ORCHESTRATOR'] == 'local'
    assert env['FORGE_VALIDATION_MODE'] == 'false'
    assert env['DATABASE_URL'] == 'sqlite:///' + str(stage / 'forge.db')
    assert env['ALLOWED_ORIGINS'] == 'http://127.0.0.1:49152'
    command['port'] = 80
    with pytest.raises(ValueError, match='端口'):
        store.serve_environment(command)
