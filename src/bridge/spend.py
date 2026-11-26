"""The guild page's Spend this week block: read from a JSON file another process writes, never written here."""

import json
from datetime import datetime
from pathlib import Path
from typing import cast

STALE_AFTER_MS = 3 * 3_600_000


def _ms(value: object) -> int | None:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _rows(raw: object) -> list[dict[str, object]]:
    items = cast(list[object], raw) if isinstance(raw, list) else []
    return [cast(dict[str, object], row) for row in items if isinstance(row, dict)]


def _map_share_row(name: str, raw: object) -> dict[str, object]:
    row = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    return {"name": name, "share": _number(row.get("share")), "est_pct": _number(row.get("est_pct_of_active_week"))}


def map_file_to_spend(path: str, now: int) -> dict[str, object] | None:
    """None: no file set, or it is missing or unreadable. A file older than STALE_AFTER_MS gives only its age."""
    if not path:
        return None
    try:
        raw = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    raw = cast(dict[str, object], raw)
    made = _ms(raw.get("generated_at"))
    if made is None:
        return None
    if now - made > STALE_AFTER_MS:
        return {"stale_hours": (now - made) // 3_600_000}
    page = raw.get("page")
    return {
        "accounts": [
            {
                "id": str(a.get("id", "")),
                "active": a.get("active") is True,
                "weekly_pct": _number(a.get("weekly_pct")) if a.get("weekly_pct") is not None else None,
                "resets_at": _ms(a.get("resets_at")),
            }
            for a in _rows(raw.get("accounts"))
        ],
        "rows": [_map_share_row(str(lead.get("lead", "")), lead) for lead in _rows(raw.get("leads"))],
        "harness": _map_share_row("harness", raw.get("harness")),
        "other": _map_share_row("other", raw.get("other")),
        "page": page if isinstance(page, str) and page.startswith(("http://", "https://")) else "",
    }
