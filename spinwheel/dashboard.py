"""Render the self-contained HTML dashboard (data embedded, no server, no network)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .analysis import SHORT_MAX_SECONDS, build_dashboard_data, to_json

TEMPLATE = Path(__file__).with_name("dashboard.html")
PLACEHOLDER = "/*__SPINWHEEL_DATA__*/null"
LIVE_PLACEHOLDER = "/*__SPINWHEEL_LIVE__*/false"


def render(conn: sqlite3.Connection, short_max: int = SHORT_MAX_SECONDS, live: bool = False) -> str:
    """`live` is true only when `spinwheel serve` renders the page; it turns on the Refresh button."""
    html = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in html or LIVE_PLACEHOLDER not in html:
        raise RuntimeError("dashboard template is missing its data placeholder")
    html = html.replace(LIVE_PLACEHOLDER, "true" if live else "false", 1)
    return html.replace(PLACEHOLDER, to_json(build_dashboard_data(conn, short_max)), 1)


def write(conn: sqlite3.Connection, out: str | Path, short_max: int = SHORT_MAX_SECONDS) -> Path:
    out = Path(out)
    out.write_text(render(conn, short_max), encoding="utf-8")
    return out
