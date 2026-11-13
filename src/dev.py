"""Start the local web app against the resident protocol server."""

import os
from importlib import import_module
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ui = import_module('frontend.ui-protocol')


def create_app():
    from frontend.web import create_app as web_app
    from backend import build_backend

    return web_app(build_backend(Path.cwd()))


def wait_for_server(process, host: str, port: int, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"服务启动失败，退出码 {code}。")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("等待服务启动超时。")
            time.sleep(0.1)


def service_root() -> Path | None:
    request = ui.UiRequest('service-check', 'service.info',
                           'urn:lianghua:ui:input:service.info:v1', {})
    try:
        messages = ui._send_backend(request, timeout=2)
    except (ConnectionRefusedError, TimeoutError):
        return None
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError('端口 8000 上的服务返回无效数据') from error
    if (not isinstance(messages, list) or len(messages) != 1
            or not isinstance(messages[0], ui.UiMessage)
            or messages[0].kind != 'resource'
            or messages[0].schema != 'urn:lianghua:ui:output:service.info:v1'
            or messages[0].correlation_id != 'service-check'
            or not isinstance(messages[0].data, dict)
            or not isinstance(messages[0].data.get('root'), str)):
        raise RuntimeError('端口 8000 上的服务不能验证项目身份')
    return Path(messages[0].data['root']).resolve()


def ensure_service(root: Path) -> None:
    root = root.resolve()
    actual = service_root()
    if actual is None:
        try:
            subprocess.run(['launchctl', 'kickstart', f'gui/{os.getuid()}/org.lianghua.backend'],
                           check=True, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError('常驻后端未启动，请先安装并启动登录服务') from error
        deadline = time.monotonic() + 30
        while actual is None:
            actual = service_root()
            if actual is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError('常驻后端启动超时，请检查 data/service.log')
                time.sleep(0.1)
    if actual != root:
        raise RuntimeError(f'后端项目不匹配：{actual}，当前项目：{root}')


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
        ensure_service(root)
        process = subprocess.Popen(
            ["npm", "--prefix", str(web), "run", "dev", "--", "--strictPort"],
            cwd=root, start_new_session=True)
        processes.append(process)
        print("等待前端启动…", flush=True)
        wait_for_server(process, "127.0.0.1", 3000)
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
