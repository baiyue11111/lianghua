"""Start the local web app and its protocol server together."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def create_app():
    from frontend.web import create_app as web_app
    from backend import build_backend

    return web_app(build_backend(Path.cwd()))


def main() -> int:
    root = Path.cwd()
    web = root / "frontend" / "web"
    if not (root / "pyproject.toml").is_file() or not (web / "package.json").is_file():
        print("请在 lianghua 项目根目录运行。", file=sys.stderr)
        return 1
    processes = []

    def stop(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        commands = [
            [sys.executable, "-m", "uvicorn", "dev:create_app", "--factory",
             "--host", "127.0.0.1", "--port", "8000"],
            ["npm", "--prefix", str(web), "run", "dev", "--", "--strictPort"],
        ]
        for command in commands:
            processes.append(subprocess.Popen(command, cwd=root, start_new_session=True))
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    return code if code else 1
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        interrupt = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
