"""Start services with one command.

The launcher knows only endpoints, one data file and commands.  It checks what
is already answered and starts only what is missing.  What a service does
behind the port — scheduling, collection, protocols — is none of the launcher's
business, and a service already running is left untouched.

The data file is the one thing that cannot be reused by opening it a second
time: DuckDB is embedded and takes an exclusive lock, so a second process
cannot attach at all — not even read-only.  What is reusable is the *process*
that holds it, and that is the backend: backtests and collections ask it over
the protocol instead of opening the file themselves.  So the check is a probe,
not a wait — a busy file means a migration tool, which is a person's thing to
finish, not the launcher's.
"""

import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time


CATALOG = Path("data") / "catalog.duckdb"
STEPS = 3


def say(step: int, message: str) -> None:
    print(f"[{step}/{STEPS}] {message}", flush=True)


def create_app():
    """Backend ASGI factory; used by launchd and by the local launcher."""
    from frontend.web import create_app as web_app
    from backend import build_backend

    return web_app(build_backend(Path.cwd()))


def port_open(host: str, port: int) -> bool:
    """Whether something already answers on this loopback endpoint."""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def wait_for_port(process, host: str, port: int, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"服务启动失败，退出码 {code}。")
        if port_open(host, port):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待 {host}:{port} 启动超时。")
        time.sleep(0.1)


def ensure(host: str, port: int, command: list[str], cwd: Path,
           processes: list[subprocess.Popen]) -> bool:
    """Reuse a live endpoint, otherwise start the command and wait for it.

    Returns whether the command was started here.
    """
    if port_open(host, port):
        return False
    process = subprocess.Popen(command, cwd=cwd, start_new_session=True)
    processes.append(process)
    wait_for_port(process, host, port)
    return True


def catalog_holder(path: Path) -> str | None:
    """The pid holding the catalog lock, or ``None`` when the file is free.

    Opening the file read-only is the probe because it is the same path the
    backend takes: a writer holds an exclusive lock, so a read-only attach
    fails with the same ``Conflicting lock`` error.  A missing file is free —
    the backend creates it.
    """
    if not path.exists():
        return None
    try:
        import duckdb
    except ImportError:
        return None
    try:
        duckdb.connect(str(path), read_only=True).close()
        return None
    except duckdb.Error as error:
        text = str(error)
        if "lock" not in text.lower():
            return None                 # not contention; let the backend report it
        match = re.search(r"PID (\d+)", text)
        return match.group(1) if match else "?"


def describe(pid: str) -> str:
    """A short command line for a pid, so a refusal says what to close."""
    if pid == "?":
        return ""
    try:
        result = subprocess.run(["ps", "-p", pid, "-o", "command="],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(result.stdout.split())[:100]


def require_catalog(path: Path) -> None:
    """Refuse to start the backend while another process owns the catalog.

    DuckDB is embedded: a writer takes an exclusive lock and a second process
    cannot attach at all — not even read-only. Nothing else opens the file now
    that backtests and collections run inside the backend, so a busy catalog
    means a migration tool, and waiting for it would be waiting on a person.
    """
    pid = catalog_holder(path)
    if pid is None:
        return
    holder = describe(pid)
    raise RuntimeError(
        f"数据库被另一个进程占用（PID {pid}{'：' + holder if holder else ''}）。"
        " 它通常是一次性迁移工具，等它结束后再启动。")


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
        if port_open("127.0.0.1", 8000):
            say(1, "数据库已就绪（由已运行的后端持有）。")
            say(2, "后端已启动，复用现有进程。")
        else:
            # One writer, one backend: the catalog must be free before the
            # backend opens it, and the backend then keeps it for every task.
            require_catalog(root / CATALOG)
            say(1, "数据库已就绪。")
            say(2, "启动后端…")
            ensure("127.0.0.1", 8000,
                   [sys.executable, "-m", "uvicorn", "dev:create_app", "--factory",
                    "--host", "127.0.0.1", "--port", "8000"], root, processes)
            say(2, "后端已启动。")
        say(3, "启动前端…")
        ensure("127.0.0.1", 3000,
               ["npm", "--prefix", str(web), "run", "dev", "--", "--strictPort"],
               root, processes)
        say(3, "前端已启动：http://127.0.0.1:3000/")
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
