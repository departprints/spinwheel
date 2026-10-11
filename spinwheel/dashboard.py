"""Render the self-contained HTML dashboard (data embedded, no server, no network)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .analysis import SHORT_MAX_SECONDS, build_dashboard_data, to_json

TEMPLATE = Path(__file__).with_name("dashboard.html")
PLACEHOLDER = "/*__SPINWHEEL_DATA__*/null"


def render(conn: sqlite3.Connection, short_max: int = SHORT_MAX_SECONDS) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        raise RuntimeError("dashboard template is missing its data placeholder")
    return html.replace(PLACEHOLDER, to_json(build_dashboard_data(conn, short_max)), 1)


def write(conn: sqlite3.Connection, out: str | Path, short_max: int = SHORT_MAX_SECONDS) -> Path:
    out = Path(out)
    out.write_text(render(conn, short_max), encoding="utf-8")
    return out
