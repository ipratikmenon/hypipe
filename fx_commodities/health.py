"""Health endpoint (REQUIREMENTS.md §12).

Reads the recorder's heartbeat file and the journal — deliberately does NOT
talk to the MT5 terminal itself, so it can run anywhere and can't disturb the
single-threaded adapter.

Run:  python -m fx_commodities.health
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI

from common.journal import Journal

from .config import load_settings

app = FastAPI(title="fx_commodities health", version="1.0")
_cfg = load_settings()


@app.get("/health")
def health() -> dict:
    out: dict = {"ts": datetime.now(timezone.utc).isoformat()}

    hb_path = Path(_cfg.data_root) / "heartbeat.json"
    if hb_path.exists():
        hb = json.loads(hb_path.read_text())
        out["recorder"] = hb
        hb_ts = datetime.fromisoformat(hb["ts"])
        age_s = (datetime.now(timezone.utc) - hb_ts).total_seconds()
        out["recorder_heartbeat_age_s"] = round(age_s, 1)
        out["recorder_stale"] = age_s > 600
    else:
        out["recorder"] = None
        out["recorder_stale"] = True

    if _cfg.journal_path.exists():
        j = Journal(_cfg.journal_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out["risk"] = j.get_risk_state(today)
        out["open_positions"] = j.get_open_positions()
        j.close()

    return out


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=_cfg.health_port)


if __name__ == "__main__":
    main()
