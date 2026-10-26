"""Start the local web app and its protocol server together."""

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


def create_app():
    from frontend.web import create_app as web_app
    from backend import build_backend

    return web_app(build_backend(Path.cwd()))


def wait_for_server(process, host: str, port: int, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"后端启动失败，退出码 {code}。")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("等待后端启动超时。")
            time.sleep(0.1)


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
        host, port = "127.0.0.1", 8000
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind((host, port))
        commands = [
            [sys.executable, "-m", "uvicorn", "dev:create_app", "--factory",
             "--host", host, "--port", str(port)],
            ["npm", "--prefix", str(web), "run", "dev", "--", "--strictPort"],
        ]
        processes.append(subprocess.Popen(commands[0], cwd=root, start_new_session=True))
        print("等待后端启动…", flush=True)
        wait_for_server(processes[0], host, port)
        processes.append(subprocess.Popen(commands[1], cwd=root, start_new_session=True))
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    return code if code else 1
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return 1
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
