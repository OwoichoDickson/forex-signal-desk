"""Configuration: config.yaml + optional .env + environment-variable overrides."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

ENV_OVERRIDES = {
    "FXSIG_ACCOUNT_CURRENCY": (("risk", "account_currency"), str),
    "FXSIG_RISK_PER_TRADE_PCT": (("risk", "risk_per_trade_pct"), float),
    "FXSIG_DAILY_LOSS_USED_PCT": (("risk", "daily_loss_used_pct"), float),
    "FXSIG_SCAN_INTERVAL_MINUTES": (("scan", "interval_minutes"), int),
    "FXSIG_LOG_LEVEL": (("logging", "level"), str),
    "TV_MCP_SERVER_PATH": (("mcp", "server_path"), str),
    "FXSIG_DASHBOARD_PORT": (("dashboard", "port"), int),
    "FXSIG_ADMIN_EMAILS": (("dashboard", "admin_emails"), lambda v: [e.strip() for e in v.split(",") if e.strip()]),
    "FXSIG_PUBLIC_URL": (("dashboard", "public_url"), str),
}


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    for env, (keys, cast) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            node = cfg
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = cast(os.environ[env])
    for key in ("output_dir", "state_dir", "log_dir"):
        p = Path(cfg["paths"][key]).expanduser()
        cfg["paths"][key] = str(p if p.is_absolute() else ROOT / p)
    return cfg


def all_pairs(cfg: dict) -> list[str]:
    p = cfg["pairs"]
    return [*p["majors"], *p["crosses"], *p["metals"]]
