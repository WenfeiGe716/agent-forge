"""Opt-in read-race probe using synthetic files, with isolated Forge settings.

No sensitive file, network, model call, or production task is involved.
"""
import asyncio
import dataclasses
import json
import os
import shlex
import signal
import tempfile
from pathlib import Path

import conftest  # noqa: F401
from forge import local_executor, tools


async def main():
    with tempfile.TemporaryDirectory(prefix='forge-read-race-probe-') as directory:
        base = Path(directory).resolve()
        root = base / 'task'; root.mkdir()
        outside = base / 'outside-fixture.txt'
        outside.write_text('SYNTHETIC-UNAPPROVED-CONTENT')
        (root / 'race.txt').write_text('ordinary workspace data')
        script = root / 'race_probe.py'
        script.write_text('''import os, time
from pathlib import Path
if os.fork():
    for _ in range(100):
        if Path('detached.pid').exists(): break
        time.sleep(.01)
    os._exit(0)
os.setsid()
Path('detached.pid').write_text(str(os.getpid()))
for fd in (0, 1, 2):
    try: os.close(fd)
    except OSError: pass
end = time.monotonic() + 3
while time.monotonic() < end:
    Path('swap-plain.txt').write_text('ordinary workspace data')
    os.replace('swap-plain.txt', 'race.txt')
    os.symlink(OUTSIDE, 'swap-link.txt')
    os.replace('swap-link.txt', 'race.txt')
os._exit(0)
'''.replace('OUTSIDE', repr(str(outside))))
        local_executor.settings = dataclasses.replace(local_executor.settings, local_execution_enabled=True)
        pid = None
        try:
            result = await local_executor.spawn(root, 'python3 ' + shlex.quote(str(script)),
                                                {'command_timeout_s': 1}, 'ask')
            pid_file = root / 'detached.pid'
            if pid_file.exists(): pid = int(pid_file.read_text())
            observed = False
            for index in range(20000):
                try:
                    read = await tools.invoke('read_file', {'path': 'race.txt'}, ['read_file'], root,
                                              'a' * 32, 'work', 'probe:' + str(index),
                                              {'max_file_bytes': 2000}, 'unused')
                    if read['content'] == 'SYNTHETIC-UNAPPROVED-CONTENT':
                        observed = True
                        break
                except (ValueError, OSError):
                    pass
            print(json.dumps({'native_exit': result['exit_code'], 'native_default_network': False,
                              'extra_file_grants': [], 'unapproved_read_observed': observed,
                              'read_attempts': index + 1}, ensure_ascii=False, indent=2))
        finally:
            if pid:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass


if __name__ == '__main__':
    asyncio.run(main())
