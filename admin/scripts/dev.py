"""Run the seeded backend and Vite HMR as one cross-platform process group."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from types import FrameType
from typing import cast

ADMIN_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ADMIN_DIR / "frontend"


def _spawn(command: list[str], cwd: Path, env: Mapping[str, str]) -> subprocess.Popen[bytes]:
    if os.name == "nt":
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    os.kill(-process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.kill(-process.pid, 9)


def _interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(cast(signal.Signals, sigbreak), _interrupt)


def main() -> int:
    _install_signal_handlers()
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        print("ERROR: npm was not found on PATH", file=sys.stderr)
        return 1
    backend_port = os.environ.get("IH_ADMIN_DEV_BACKEND_PORT", "8765")
    frontend_port = os.environ.get("IH_ADMIN_DEV_FRONTEND_PORT", "5173")
    child_env = os.environ.copy()
    child_env["VITE_BACKEND_ORIGIN"] = f"http://127.0.0.1:{backend_port}"
    commands = (
        (
            [
                sys.executable,
                "-m",
                "litestar",
                "--app",
                "identity_hub_admin.dev:app",
                "run",
                "--reload",
                "--host",
                "127.0.0.1",
                "--port",
                backend_port,
            ],
            ADMIN_DIR,
            child_env,
        ),
        (
            [npm, "run", "dev", "--", "--port", frontend_port, "--strictPort"],
            FRONTEND_DIR,
            child_env,
        ),
    )
    processes: list[subprocess.Popen[bytes]] = []
    try:
        processes = [_spawn(command, cwd, env) for command, cwd, env in commands]
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    return code
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 130
    finally:
        for process in reversed(processes):
            _stop(process)


if __name__ == "__main__":
    raise SystemExit(main())
