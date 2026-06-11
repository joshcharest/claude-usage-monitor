"""Console-script entry for the dashboard.

  claude-usage-dashboard            # start the Streamlit server
  claude-usage-dashboard --status   # report whether it's running
  claude-usage-dashboard --stop     # gracefully stop it (SIGTERM, then SIGKILL)

Any other args are passed through to `streamlit run`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def _app_path() -> Path:
    return Path(__file__).with_name("app.py")


# Matches the server whether launched with a relative path (ensure-dashboard.sh
# cd's into the repo) or absolute — both command lines contain this tail.
_MATCH = "claude_usage_monitor/dashboard/app.py"


def _port() -> int:
    try:
        return int(os.environ.get("CLAUDE_USAGE_DASHBOARD_PORT", "8501"))
    except ValueError:
        return 8501


def _server_pids() -> list[int]:
    """PIDs of dashboard server processes (matched by app.py path).

    Excludes processes in this caller's own process group — that drops the
    invoking shell or wrapper that merely mentions the path on its command line.
    The real server is detached via ``setsid`` into its own session, so it is
    never in the caller's group.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", _MATCH],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return []
    my_pgid = os.getpgrp()
    pids = []
    for line in out.stdout.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            if os.getpgid(pid) == my_pgid:
                continue
        except ProcessLookupError:
            continue
        pids.append(pid)
    return pids


def _is_healthy(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as resp:
            return resp.status == 200
    except (URLError, OSError):
        return False


def status() -> int:
    port = _port()
    pids = _server_pids()
    healthy = _is_healthy(port)
    if healthy or pids:
        print(f"dashboard running on http://127.0.0.1:{port} "
              f"(pids: {', '.join(map(str, pids)) or 'unknown'})")
    else:
        print("dashboard not running")
    return 0


def stop() -> int:
    port = _port()
    pids = _server_pids()
    if not pids and not _is_healthy(port):
        print("dashboard not running")
        return 0

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    # Wait for a graceful shutdown.
    for _ in range(20):
        if not _server_pids() and not _is_healthy(port):
            print("dashboard stopped")
            return 0
        time.sleep(0.25)

    # Force-kill anything still alive.
    leftover = _server_pids()
    for pid in leftover:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    print("dashboard stopped" + (" (forced)" if leftover else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if "--stop" in argv:
        return stop()
    if "--status" in argv:
        return status()

    app_path = _app_path()
    try:
        from streamlit.web import cli as stcli
    except ModuleNotFoundError:
        print(
            "The dashboard extra is not installed. Run:\n"
            "  uv sync --extra dashboard\n"
            "then:\n"
            "  uv run --extra dashboard claude-usage-dashboard",
            file=sys.stderr,
        )
        return 1
    sys.argv = ["streamlit", "run", str(app_path), *argv]
    return stcli.main()  # type: ignore[no-any-return]


if __name__ == "__main__":
    raise SystemExit(main())
