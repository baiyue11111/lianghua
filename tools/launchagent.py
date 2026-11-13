"""Render the per-user launchd service configuration for this checkout."""

from pathlib import Path
import plistlib
import sys


def build(root: Path) -> bytes:
    root = root.resolve()
    python = root / '.venv' / 'bin' / 'python'
    if not python.is_file():
        raise ValueError('项目虚拟环境缺少 Python：' + str(python))
    return plistlib.dumps({
        'Label': 'org.lianghua.backend',
        'ProgramArguments': [str(python), '-m', 'uvicorn', 'dev:create_app', '--factory',
                             '--host', '127.0.0.1', '--port', '8000'],
        'WorkingDirectory': str(root),
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'StandardErrorPath': str(root / 'data' / 'service.log'),
    }, sort_keys=False)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('用法：python tools/launchagent.py <输出路径>')
    Path(sys.argv[1]).write_bytes(build(Path(__file__).resolve().parent.parent))
