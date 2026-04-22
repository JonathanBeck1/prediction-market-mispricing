#!/usr/bin/env python3
from __future__ import annotations

import argparse
import plistlib
import re
import subprocess
import time
from pathlib import Path


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(label: str) -> Path:
    return _launch_agents_dir() / f"{label}.plist"


def _write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(payload, f)


def _run_launchctl(args: list[str]) -> None:
    subprocess.run(["launchctl", *args], check=False)


def _launchctl_list(label: str) -> str:
    proc = subprocess.run(
        ["launchctl", "list", label],
        check=False,
        capture_output=True,
        text=True,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _job_last_exit_status(label: str) -> int | None:
    text = _launchctl_list(label)
    m = re.search(r'"LastExitStatus"\s*=\s*(\d+);', text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _job_pid(label: str) -> int | None:
    text = _launchctl_list(label)
    m = re.search(r'"PID"\s*=\s*(\d+);', text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _is_protected_tcc_path(path: Path) -> bool:
    home = Path.home()
    protected_roots = [
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
    ]
    return any(path.is_relative_to(root) for root in protected_roots)


def _print_post_install_status(label: str, repo_root: Path) -> None:
    # Give launchd a moment to spawn once so status reflects reality.
    time.sleep(0.6)
    pid = _job_pid(label)
    exit_status = _job_last_exit_status(label)
    if pid:
        print(f"Service running: {label} (pid {pid})")
        return
    if exit_status in (None, 0):
        print(f"Service loaded: {label}")
        return

    print(f"WARNING: {label} loaded but not running (LastExitStatus={exit_status}).")
    if _is_protected_tcc_path(repo_root):
        print(
            "macOS privacy likely blocked launchd from this repo path "
            f"({repo_root}). Use `make local-up`/`make local-restart`, or move "
            "the repo outside Desktop/Documents/Downloads."
        )


def _write_run_script(repo_root: Path, script_name: str, cmd: str) -> Path:
    """Write a shell launcher script and make it executable.

    launchd agents launched directly from a venv Python binary fail on macOS
    with PermissionError reading pyvenv.cfg (TCC sandboxing).  Launching via
    /bin/zsh with explicit venv activation works around this reliably.

    zsh does not support `exec VAR=val cmd` syntax — inline env assignments
    before exec are a bash-ism.  Use `export` statements before `exec` instead.
    """
    script = repo_root / "scripts" / script_name
    # Split "KEY=val KEY2=val2 python3 ..." into separate export lines + exec.
    parts = cmd.split()
    env_exports: list[str] = []
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        env_exports.append(f"export {parts.pop(0)}")
    exec_cmd = " ".join(parts)
    env_lines = "\n".join(env_exports)
    script.write_text(
        "#!/bin/zsh\n"
        f'cd "{repo_root}"\n'
        f'source "{repo_root}/.venv/bin/activate"\n'
        f"{env_lines}\n"
        f"exec {exec_cmd}\n"
    )
    script.chmod(0o755)
    return script


def _build_runner_plist(repo_root: Path, python_bin: Path, label: str) -> dict:
    logs_dir = repo_root / "data" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    launch_script = _write_run_script(
        repo_root,
        "launchd_run_runner.sh",
        (
            "KALSHI_MOCK=0 MAINTENANCE_ENABLED=1 "
            "PRE_EVENT_WINDOW_SEC=604800 "
            "FETCH_MARKETS_INTERVAL_SEC=600 "
            "python3 -m app.runner"
        ),
    )
    return {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", str(launch_script)],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs_dir / "runner.out.log"),
        "StandardErrorPath": str(logs_dir / "runner.err.log"),
    }


def _build_dashboard_plist(repo_root: Path, python_bin: Path, label: str, port: int) -> dict:
    logs_dir = repo_root / "data" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    launch_script = _write_run_script(
        repo_root,
        "launchd_run_dashboard.sh",
        f"python3 -m app.dashboard --port {port} --no-open",
    )
    return {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", str(launch_script)],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs_dir / "dashboard.out.log"),
        "StandardErrorPath": str(logs_dir / "dashboard.err.log"),
    }


def install(repo_root: Path, with_dashboard: bool, dashboard_port: int) -> None:
    python_bin = repo_root / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        raise SystemExit(f"Missing venv python: {python_bin}")

    runner_label = "com.kalshi-edge.runner"
    runner_plist = _plist_path(runner_label)
    _write_plist(runner_plist, _build_runner_plist(repo_root, python_bin, runner_label))
    _run_launchctl(["unload", str(runner_plist)])
    _run_launchctl(["load", "-w", str(runner_plist)])
    print(f"Installed and loaded: {runner_label}")
    _print_post_install_status(runner_label, repo_root)

    if with_dashboard:
        dash_label = "com.kalshi-edge.dashboard"
        dash_plist = _plist_path(dash_label)
        _write_plist(
            dash_plist,
            _build_dashboard_plist(repo_root, python_bin, dash_label, dashboard_port),
        )
        _run_launchctl(["unload", str(dash_plist)])
        _run_launchctl(["load", "-w", str(dash_plist)])
        print(f"Installed and loaded: {dash_label} (port {dashboard_port})")
        _print_post_install_status(dash_label, repo_root)

    print("Check services: launchctl list | rg kalshi-edge")


def uninstall(with_dashboard: bool) -> None:
    labels = ["com.kalshi-edge.runner"]
    if with_dashboard:
        labels.append("com.kalshi-edge.dashboard")
    for label in labels:
        plist_path = _plist_path(label)
        _run_launchctl(["unload", str(plist_path)])
        if plist_path.exists():
            plist_path.unlink()
        print(f"Uninstalled: {label}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install/uninstall 24x7 launchd services for kalshi-edge")
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to repo root (default: current directory)",
    )
    parser.add_argument(
        "--with-dashboard",
        action="store_true",
        help="Also install dashboard service",
    )
    parser.add_argument("--dashboard-port", type=int, default=8777)
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if args.action == "install":
        install(repo_root, args.with_dashboard, args.dashboard_port)
        return
    uninstall(args.with_dashboard)


if __name__ == "__main__":
    main()
