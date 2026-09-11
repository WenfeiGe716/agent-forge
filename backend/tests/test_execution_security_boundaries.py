"""Safe native-boundary probes: synthetic files only, no external network or models."""
import dataclasses
import shlex
import sys
from pathlib import Path

import pytest

from forge import local_executor
from forge.execution_auto_review import manual_reason


@pytest.fixture
def native(monkeypatch):
    if sys.platform != 'darwin' or not Path(local_executor.SANDBOX).is_file():
        pytest.skip('macOS Seatbelt is required for this OS boundary probe')
    monkeypatch.setattr(local_executor, 'settings', dataclasses.replace(local_executor.settings, local_execution_enabled=True))
    return local_executor


def request(command, **kwargs):
    return {'command': command, 'network': True, 'read_paths': [], 'write_paths': [],
            'reason': 'Install a task-local fixture dependency', **kwargs}


@pytest.mark.parametrize('command', [
    './python3 -m pip install --target deps safe-fixture==1.2.3',
    './python3 -I -m pip install --only-binary=:all: --no-deps --target deps safe-fixture==1.2.3',
    '/tmp/python3 -I -m pip install --only-binary=:all: --no-deps --target deps safe-fixture==1.2.3',
    './npm install safe-fixture@1.2.3 --ignore-scripts',
    'python3 -m pip install --target deps safe-fixture==1.2.3',
    'python3 -I -m pip install --target deps safe-fixture==1.2.3',
    'npm install safe-fixture@1.2.3 --ignore-scripts',
])
def test_auto_never_admits_opaque_launchers_or_unreviewed_install_code(command):
    assert manual_reason(request(command)), command


@pytest.mark.asyncio
async def test_python_module_shadow_is_real_and_isolated_mode_avoids_it(native, tmp_path):
    root = tmp_path / 'task'; root.mkdir()
    # This stand-in performs no package resolution or network operation.
    (root / 'pip.py').write_text("from pathlib import Path\nPath('module-shadow-marker').write_text('synthetic fixture')\n")
    result = await native.spawn(root, 'python3 -m pip install --target deps safe-fixture==1.2.3',
                                {'command_timeout_s': 5}, 'auto')
    assert result['exit_code'] == 0, result
    assert (root / 'module-shadow-marker').read_text() == 'synthetic fixture'
    (root / 'module-shadow-marker').unlink()
    await native.spawn(root, 'python3 -I -m pip --version', {'command_timeout_s': 5}, 'auto')
    assert not (root / 'module-shadow-marker').exists()


@pytest.mark.asyncio
async def test_literal_write_grant_does_not_expose_parent_or_siblings(native, tmp_path):
    root = tmp_path / 'task'; root.mkdir()
    external = tmp_path / 'external'; external.mkdir()
    target = external / 'approved-output.txt'
    target.write_text('before')
    sibling = external / 'unapproved-input.txt'
    sibling.write_text('synthetic sibling fixture')
    paths = native.canonical_grants([{'path': str(target), 'access': 'write'}])
    # Temporary grants prohibit fork; use the shell builtin for this write.
    write = await native.spawn(root, 'printf "after\\n" > ' + shlex.quote(str(target)),
                               {'command_timeout_s': 5}, 'ask', paths=paths)
    assert write['exit_code'] == 0, write
    assert target.read_text() == 'after\n'
    read = await native.spawn(root, '/bin/cat ' + shlex.quote(str(sibling)),
                              {'command_timeout_s': 5}, 'ask', paths=paths)
    assert read['exit_code'] != 0 and 'synthetic sibling fixture' not in read['output'], read
    listing = await native.spawn(root, '/bin/ls ' + shlex.quote(str(external)),
                                 {'command_timeout_s': 5}, 'ask', paths=paths)
    assert listing['exit_code'] != 0 and 'unapproved-input.txt' not in listing['output'], listing


def test_external_grants_reject_symlink_hardlink_and_directory(tmp_path):
    original = tmp_path / 'original.txt'; original.write_text('synthetic fixture')
    link = tmp_path / 'link.txt'; link.symlink_to(original)
    with pytest.raises(ValueError): local_executor.canonical_grants([{'path': str(link), 'access': 'read'}])
    hardlink = tmp_path / 'hardlink.txt'; hardlink.hardlink_to(original)
    with pytest.raises(ValueError): local_executor.canonical_grants([{'path': str(hardlink), 'access': 'read'}])
    with pytest.raises(ValueError): local_executor.canonical_grants([{'path': str(tmp_path), 'access': 'write'}])


@pytest.mark.asyncio
async def test_sandbox_cannot_import_unapproved_file_by_hardlink(native, tmp_path):
    root = tmp_path / 'task'; root.mkdir()
    outside = tmp_path / 'outside-data.txt'; outside.write_text('synthetic outside fixture')
    result = await native.spawn(root, '/bin/ln ' + shlex.quote(str(outside)) + ' inside-copy.txt',
                                {'command_timeout_s': 5}, 'ask')
    assert result['exit_code'] != 0, result
    assert not (root / 'inside-copy.txt').exists()
