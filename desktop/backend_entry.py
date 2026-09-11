"""Private stdin-only launcher used by the Electron main process and frozen bundle."""
import json
import os
from pathlib import Path
import sys
import threading
import time

from data_store import prepare, serve_environment


def main():
    os.umask(0o077)
    try:
        line = sys.stdin.readline(262145)
        if len(line) > 262144:
            raise ValueError('桌面启动参数过长')
        message = json.loads(line)
        if message.get('action') == 'prepare':
            print(json.dumps(prepare(message['data_dir'], message.get('import_from')), ensure_ascii=False), flush=True)
            return 0
        if message.get('action') != 'serve':
            raise ValueError('未知桌面启动动作')
        env = serve_environment(message)
        if getattr(sys, 'frozen', False):
            frontend = Path(sys._MEIPASS) / 'frontend'
        else:
            repo = Path(__file__).resolve().parents[1]
            sys.path.insert(0, str(repo / 'backend'))
            frontend = repo / 'frontend' / 'dist-desktop'
        if not (frontend / 'index.html').is_file():
            raise ValueError('桌面界面资源缺失，请重新构建客户端')
        # Clear externally supplied bootstrap switches. The desktop owns its runner.
        os.environ.pop('FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION', None)
        os.environ.update(env, FRONTEND_DIR=str(frontend))
        import uvicorn
        from forge.api import app
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=message['port'],
                                access_log=False, log_level='warning', timeout_graceful_shutdown=5))
        parent = os.getppid()
        def watch_parent():
            while not server.should_exit:
                time.sleep(1)
                if os.getppid() != parent:
                    server.should_exit = True
                    return
        threading.Thread(target=watch_parent, daemon=True).start()
        server.run()
        return 0 if server.started else 1
    except Exception as exc:
        # Values and tracebacks can contain configured secrets. Only deliberate
        # validation messages are safe to return over the private protocol.
        safe = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else '桌面数据准备或启动失败，请检查本地资源与配置完整性'
        print(json.dumps({'ok': False, 'error': safe}, ensure_ascii=False), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
