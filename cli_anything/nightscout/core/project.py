"""Connection config + session-state management.

Resolution order for connection settings (highest precedence first):

1. CLI flags (`--url`, `--api-secret`, `--token`)
2. Environment vars (`NIGHTSCOUT_URL`, `NIGHTSCOUT_API_SECRET`, `NIGHTSCOUT_TOKEN`)
3. Persisted config at `~/.cli-anything/nightscout/config.json`

Sessions track the resolved connection plus a small fetch cache (most recent
entries / treatments / etc.) so that the REPL can show stateful context and
so that one-shot mutations can auto-save.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(os.environ.get("CLI_ANYTHING_HOME", str(Path.home() / ".cli-anything"))) / "nightscout"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_SESSION_FILE = CONFIG_DIR / "session.json"

DEFAULTS: dict[str, Any] = {
    "server_url": "",
    "api_secret": "",
    "api_token": "",
    "units": "mg/dl",
}


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    try:
        # 0o700 (rwx------) is owner-only — the most restrictive standard
        # directory mode. 0o644 would strip the execute bit and make the
        # directory untraversable.  nosemgrep: python.lang.security.audit.insecure-file-permissions
        os.chmod(str(p), 0o700)
    except OSError:
        pass


def load_config() -> dict[str, Any]:
    """Load saved config, then overlay env vars on top."""
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, ValueError):
            pass
    if v := os.environ.get("NIGHTSCOUT_URL"):
        cfg["server_url"] = v
    if v := os.environ.get("NIGHTSCOUT_API_SECRET"):
        cfg["api_secret"] = v
    if v := os.environ.get("NIGHTSCOUT_TOKEN"):
        cfg["api_token"] = v
    if v := os.environ.get("NIGHTSCOUT_UNITS"):
        cfg["units"] = v
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    """Persist config to disk with restricted permissions."""
    _ensure_dir(CONFIG_DIR)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(str(CONFIG_FILE), 0o600)
    except OSError:
        pass
    return CONFIG_FILE


def clear_config() -> None:
    """Remove the on-disk config (env vars still apply)."""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()


def get_connection(
    url: str | None = None,
    api_secret: str | None = None,
    token: str | None = None,
) -> dict[str, str]:
    """Resolve connection from flags > env > config."""
    cfg = load_config()
    return {
        "server_url": url or cfg.get("server_url", "") or "",
        "api_secret": api_secret or cfg.get("api_secret", "") or "",
        "api_token": token or cfg.get("api_token", "") or "",
        "units": cfg.get("units", "mg/dl"),
    }


# ─── Session state ─────────────────────────────────────────────────────────

EMPTY_SESSION: dict[str, Any] = {
    "version": 1,
    "name": "default",
    "server_url": "",
    "api_token_set": False,
    "api_secret_set": False,
    "units": "mg/dl",
    "modified": False,
    "history": [],
    "last_fetched": {
        "entries": [],
        "treatments": [],
        "devicestatus": [],
    },
}


def new_session(name: str = "default", server_url: str = "", units: str = "mg/dl") -> dict[str, Any]:
    s = {
        **EMPTY_SESSION,
        "history": [],
        "last_fetched": {"entries": [], "treatments": [], "devicestatus": []},
    }
    s["name"] = name
    s["server_url"] = server_url
    s["units"] = units
    return s


def load_session(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_SESSION_FILE
    if not p.exists():
        return new_session()
    try:
        with open(p) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return new_session()
    base = new_session()
    base.update(data)
    base["last_fetched"] = {**base.get("last_fetched", {}), **data.get("last_fetched", {})}
    return base


def save_session(session: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
    """Atomic write: tmp file + rename."""
    p = Path(path) if path else DEFAULT_SESSION_FILE
    _ensure_dir(p.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".session-", suffix=".json", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(session, f, indent=2, default=str)
        os.replace(tmp_name, str(p))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass
    return p


def record_history(session: dict[str, Any], action: str, detail: str = "") -> None:
    h = session.setdefault("history", [])
    h.append({"action": action, "detail": detail})
    if len(h) > 200:
        del h[: len(h) - 200]
