"""Start the refresh helper (`spinwheel serve`) automatically at login, on macOS, via a launchd agent.

launchd gives agents a minimal PATH and no working directory, so the plist pins everything the helper
needs: this Python, the package location, the database and dashboard paths, and the yt-dlp executable.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.spinwheel.helper"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "spinwheel.log"
EXTRA_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def build_plist(*, python: str, package_root: str, workdir: str, db_path: str, output: str, yt_dlp: str,
                port: int, extra_args: list[str], log: str = str(LOG)) -> dict:
    args = [python, "-m", "spinwheel", "--db", db_path, "serve", "--port", str(port), "-o", output, "--yt-dlp", yt_dlp]
    if extra_args:
        args += ["--yt-dlp-args", " ".join(extra_args)]
    return {
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": workdir,
        "EnvironmentVariables": {"PYTHONPATH": package_root, "PATH": EXTRA_PATH},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ValueError("autostart is only implemented for macOS (launchd). On other systems, run `python -m spinwheel serve` yourself")


def install(db_path: str, output: str, *, yt_dlp: str = "yt-dlp", port: int = 8765, extra_args: list[str] | None = None) -> Path:
    _require_macos()
    exe = shutil.which(yt_dlp, path=f"{os.environ.get('PATH', '')}:{EXTRA_PATH}")
    if not exe:
        raise ValueError(f"yt-dlp not found ({yt_dlp}); install it first, or pass --yt-dlp PATH")
    plist = build_plist(python=sys.executable, package_root=str(Path(__file__).resolve().parent.parent), workdir=os.getcwd(),
                        db_path=str(Path(db_path).resolve()), output=str(Path(output).resolve()), yt_dlp=exe,
                        port=port, extra_args=extra_args or [])
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # replace a previous install; fails harmlessly if none
    r = _launchctl("bootstrap", domain, str(PLIST))
    if r.returncode != 0:
        raise ValueError(f"launchctl could not load the agent: {r.stderr.strip() or r.stdout.strip()}")
    return PLIST


def uninstall() -> bool:
    _require_macos()
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    existed = PLIST.exists()
    PLIST.unlink(missing_ok=True)
    return existed


def running() -> bool:
    return sys.platform == "darwin" and _launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0
