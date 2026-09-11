"""Opt-in safe diagnostic, not a unit-test assertion of a security guarantee.

Run from repository root with PYTHONPATH=backend .venv/bin/python
backend/tests/probe_execution_process_lifetime.py. Only temporary synthetic files
are touched; conftest isolates all Forge settings before importing the executor.
"""
import asyncio
import dataclasses
import json
import os
import shlex
import signal
import tempfile
import time
from pathlib import Path

import conftest  # noqa: F401 -- initialize isolated settings before Forge imports
from forge import local_executor


async def probe(timeout):
    with tempfile.TemporaryDirectory(prefix='forge-process-lifetime-probe-') as directory:
        base = Path(directory).resolve()
        root = base / 'task'; root.mkdir()
        grant = base / 'approved-output.txt'; grant.write_text('not written')
        script = root / 'lifetime_probe.py'
        script.write_text('''import os, time
from pathlib import Path
child = os.fork()
if child:
    for _ in range(100):
        if Path('detached.pid').exists(): break
        time.sleep(.01)
    time.sleep(PARENT_DELAY)
    os._exit(0)
os.setsid()
Path('detached.pid').write_text(str(os.getpid()))
for fd in (0, 1, 2):
    try: os.close(fd)
    except OSError: pass
time.sleep(CHILD_DELAY)
Path(GRANT_PATH).write_text('synthetic write after command finished')
os._exit(0)
'''.replace('PARENT_DELAY', '4' if timeout else '0')
           .replace('CHILD_DELAY', '1.5' if timeout else '.5')
           .replace('GRANT_PATH', repr(str(grant))))
        local_executor.settings = dataclasses.replace(local_executor.settings, local_execution_enabled=True)
        pid = None
        try:
            start = time.monotonic()
            result = await local_executor.spawn(root, 'python3 ' + shlex.quote(str(script)),
                                                {'command_timeout_s': 1}, 'ask',
                                                paths=[{'path': str(grant), 'access': 'write'}])
            finished = time.monotonic() - start
            pid_file = root / 'detached.pid'
            if pid_file.exists(): pid = int(pid_file.read_text())
            at_completion = grant.read_text()
            await asyncio.sleep(1)
            return {'timeout_probe': timeout, 'exit_code': result['exit_code'],
                    'timed_out': result['timed_out'], 'completion_seconds': round(finished, 2),
                    'at_completion': at_completion, 'after_completion': grant.read_text(),
                    'detached_pid_created': pid is not None}
        finally:
            if pid:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass


async def main():
    print(json.dumps([await probe(False), await probe(True)], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
