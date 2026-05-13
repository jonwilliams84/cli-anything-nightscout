"""cli-anything-nightscout — CLI for the Nightscout CGM remote monitor.

Talks to a real Nightscout server via its REST API. Supports both API v1
(legacy, SHA-1 hashed `api-secret` header) and API v3 (subject access token
or Bearer JWT). Designed for AI agents: every command supports `--json`.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Any

import click

from cli_anything.nightscout.core import (
    activity as activity_mod,
    devicestatus as ds_mod,
    entries as entries_mod,
    food as food_mod,
    profile as profile_mod,
    project,
    report as report_mod,
    status as status_mod,
    treatments as treatments_mod,
)
from cli_anything.nightscout.utils import nightscout_backend as backend
from cli_anything.nightscout.utils.repl_skin import ReplSkin


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
VERSION = "1.2.1"


# ─── Helpers ────────────────────────────────────────────────────────────────

def _conn(ctx: click.Context) -> dict[str, Any]:
    return dict(ctx.obj["conn"])


def _is_json(ctx: click.Context) -> bool:
    return bool(ctx.obj.get("as_json", False))


def _emit(ctx: click.Context, payload: Any, human: str | None = None) -> None:
    """Emit either JSON or a human line, then return."""
    if _is_json(ctx):
        click.echo(json.dumps(payload, indent=2, default=str))
    elif human is not None:
        click.echo(human)
    else:
        click.echo(json.dumps(payload, indent=2, default=str))


def _maybe_save_session(ctx: click.Context, action: str, detail: str = "") -> None:
    """Auto-save session after a mutation unless --dry-run is set."""
    if ctx.obj.get("dry_run"):
        return
    sess = ctx.obj.get("session")
    if sess is None:
        return
    project.record_history(sess, action, detail)
    sess["modified"] = True
    project.save_session(sess, ctx.obj.get("session_path"))


def _require_url(conn: dict[str, Any]) -> None:
    if not conn.get("server_url"):
        raise click.ClickException(
            "No Nightscout URL configured. Set NIGHTSCOUT_URL, pass --url, "
            "or run `cli-anything-nightscout config set --url ...`."
        )


def _format_entry_row(e: dict[str, Any]) -> str:
    sgv = e.get("sgv") or e.get("mbg") or "-"
    direction = e.get("direction", "")
    ds = e.get("dateString") or e.get("date", "")
    return f"  {ds:<28} {str(sgv):>5}  {direction}"


def _format_treatment_row(t: dict[str, Any]) -> str:
    et = t.get("eventType", "")
    when = t.get("created_at", "")
    bits = []
    if t.get("carbs"):
        bits.append(f"{t['carbs']}g carbs")
    if t.get("insulin"):
        bits.append(f"{t['insulin']}U insulin")
    if t.get("glucose"):
        bits.append(f"BG {t['glucose']}")
    detail = ", ".join(bits) or "(no detail)"
    return f"  {when:<28} {et:<18} {detail}"


def _hide(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}…{value[-2:]}"


# ─── Root ───────────────────────────────────────────────────────────────────

@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.option("--url", default=None, envvar="NIGHTSCOUT_URL", help="Nightscout server URL")
@click.option("--api-secret", default=None, envvar="NIGHTSCOUT_API_SECRET",
              help="API_SECRET (plaintext); will be SHA-1 hashed automatically")
@click.option("--token", default=None, envvar="NIGHTSCOUT_TOKEN",
              help="Subject access token (for v3 endpoints)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output")
@click.option("--project", "session_path", type=click.Path(dir_okay=False), default=None,
              help="Path to a session JSON file (default: ~/.cli-anything/nightscout/session.json)")
@click.option("--dry-run", is_flag=True, default=False, help="Suppress session auto-save after mutations")
@click.version_option(version=VERSION, prog_name="cli-anything-nightscout")
@click.pass_context
def cli(
    ctx: click.Context,
    url: str | None,
    api_secret: str | None,
    token: str | None,
    as_json: bool,
    session_path: str | None,
    dry_run: bool,
) -> None:
    """CLI harness for Nightscout (CGM Remote Monitor)."""
    ctx.ensure_object(dict)
    conn = project.get_connection(url=url, api_secret=api_secret, token=token)
    ctx.obj["conn"] = conn
    ctx.obj["as_json"] = as_json
    ctx.obj["dry_run"] = dry_run
    ctx.obj["session_path"] = session_path
    ctx.obj["session"] = project.load_session(session_path)
    ctx.obj["session"]["server_url"] = conn["server_url"]
    ctx.obj["session"]["api_secret_set"] = bool(conn.get("api_secret"))
    ctx.obj["session"]["api_token_set"] = bool(conn.get("api_token"))

    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


def main() -> None:
    cli()


# ─── REPL ───────────────────────────────────────────────────────────────────

@cli.command("repl", hidden=True)
@click.pass_context
def repl(ctx: click.Context) -> None:
    """Interactive REPL (default when no subcommand is given)."""
    skin = ReplSkin("nightscout", version=VERSION)
    skin.print_banner()
    conn = _conn(ctx)
    host = backend.host_label(conn["server_url"])
    if not conn["server_url"]:
        skin.warning("No Nightscout server configured. Run: config set --url https://YOUR-SITE")
    else:
        skin.info(f"Server: {host}")

    pt_session = skin.create_prompt_session()

    commands = {
        "status": "Server status / version / lastModified / verifyauth",
        "entries": "Glucose entries: latest, list, get, add, delete, slice",
        "treatments": "Treatment events: latest, list, get, add, delete",
        "profile": "Profile records: current, list",
        "devicestatus": "Device status: latest, list, delete",
        "food": "Food database: list",
        "report": "Computed reports: tir, summary, daily, gmi",
        "config": "Configure server URL / API secret / token",
        "session": "Session info / save / load / clear",
        "help": "Show this help",
        "exit / quit": "Leave the REPL",
    }

    while True:
        try:
            line = skin.get_input(
                pt_session,
                project_name=ctx.obj["session"].get("name", "default"),
                modified=ctx.obj["session"].get("modified", False),
                context=host or "",
            )
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            return
        line = (line or "").strip()
        if not line:
            continue
        if line in ("exit", "quit", ":q"):
            skin.print_goodbye()
            return
        if line in ("help", "?"):
            skin.help(commands)
            continue
        try:
            argv = shlex.split(line)
        except ValueError as e:
            skin.error(f"parse error: {e}")
            continue
        try:
            cli.main(args=argv, prog_name="", standalone_mode=False, obj=ctx.obj)
        except click.exceptions.Exit:
            pass
        except click.ClickException as e:
            skin.error(e.format_message())
        except backend.NightscoutAPIError as e:
            skin.error(str(e))
        except Exception as e:  # surface any remaining errors instead of crashing the REPL
            skin.error(f"{type(e).__name__}: {e}")


# ─── config ────────────────────────────────────────────────────────────────

@cli.group("config")
def config_grp() -> None:
    """Manage saved server URL and credentials."""


@config_grp.command("set")
@click.option("--url", default=None, help="Nightscout server URL")
@click.option("--api-secret", default=None, help="API_SECRET (plaintext)")
@click.option("--token", default=None, help="Subject access token")
@click.option("--units", type=click.Choice(["mg/dl", "mmol"]), default=None)
@click.pass_context
def config_set(ctx: click.Context, url: str | None, api_secret: str | None, token: str | None, units: str | None) -> None:
    """Persist credentials to ~/.cli-anything/nightscout/config.json."""
    cfg = project.load_config()
    if url is not None:
        cfg["server_url"] = backend.normalize_url(url)
    if api_secret is not None:
        cfg["api_secret"] = api_secret
    if token is not None:
        cfg["api_token"] = token
    if units is not None:
        cfg["units"] = units
    path = project.save_config(cfg)
    payload = {
        "config_path": str(path),
        "server_url": cfg["server_url"],
        "api_secret": _hide(cfg["api_secret"]),
        "api_token": _hide(cfg["api_token"]),
        "units": cfg["units"],
    }
    _emit(ctx, payload, human=f"saved → {path}")


@config_grp.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Show current resolved connection (secrets are masked)."""
    cfg = project.load_config()
    payload = {
        "server_url": cfg.get("server_url", ""),
        "api_secret": _hide(cfg.get("api_secret", "")),
        "api_token": _hide(cfg.get("api_token", "")),
        "units": cfg.get("units", "mg/dl"),
        "config_file": str(project.CONFIG_FILE),
    }
    if _is_json(ctx):
        _emit(ctx, payload)
    else:
        for k, v in payload.items():
            click.echo(f"  {k}: {v}")


@config_grp.command("clear")
@click.pass_context
def config_clear(ctx: click.Context) -> None:
    """Delete the saved config file (env vars still apply)."""
    project.clear_config()
    _emit(ctx, {"cleared": True}, human="config cleared")


@config_grp.command("test")
@click.pass_context
def config_test(ctx: click.Context) -> None:
    """Probe the server with current credentials."""
    conn = _conn(ctx)
    _require_url(conn)
    auth = status_mod.verifyauth(conn=conn)
    payload = {
        "server_url": conn["server_url"],
        "auth": auth,
    }
    _emit(ctx, payload, human=(
        f"  server: {conn['server_url']}\n"
        f"  message: {auth.get('message','?')}\n"
        f"  canRead: {auth.get('canRead', False)}\n"
        f"  canWrite: {auth.get('canWrite', False)}\n"
        f"  isAdmin: {auth.get('isAdmin', False)}"
    ))


# ─── status ────────────────────────────────────────────────────────────────

@cli.group("status")
def status_grp() -> None:
    """Server health and identity."""


@status_grp.command("info")
@click.pass_context
def status_info(ctx: click.Context) -> None:
    """GET /api/v1/status — server name, version, defaults."""
    conn = _conn(ctx)
    _require_url(conn)
    res = status_mod.status(conn=conn)
    _emit(ctx, res, human=(
        f"  name: {res.get('name','?')}\n"
        f"  version: {res.get('version','?')}\n"
        f"  status: {res.get('status','?')}\n"
        f"  units: {(res.get('settings',{}) or {}).get('units','?')}\n"
        f"  apiEnabled: {res.get('apiEnabled', False)}\n"
        f"  careportalEnabled: {res.get('careportalEnabled', False)}"
    ))


@status_grp.command("version")
@click.pass_context
def status_version(ctx: click.Context) -> None:
    """GET /api/v3/version."""
    conn = _conn(ctx)
    _require_url(conn)
    res = status_mod.version(conn=conn)
    _emit(ctx, res, human=(
        f"  version: {res.get('version','?')}\n"
        f"  apiVersion: {res.get('apiVersion','?')}\n"
        f"  storage: {(res.get('storage',{}) or {}).get('storage','?')}"
    ))


@status_grp.command("last-modified")
@click.pass_context
def status_last_modified(ctx: click.Context) -> None:
    """GET /api/v3/lastModified — most recent change per collection."""
    conn = _conn(ctx)
    _require_url(conn)
    res = status_mod.last_modified(conn=conn)
    _emit(ctx, res)


@status_grp.command("verifyauth")
@click.pass_context
def status_verifyauth(ctx: click.Context) -> None:
    """GET /api/v1/verifyauth — confirm credentials."""
    conn = _conn(ctx)
    _require_url(conn)
    res = status_mod.verifyauth(conn=conn)
    _emit(ctx, res)


# ─── entries ───────────────────────────────────────────────────────────────

@cli.group("entries")
def entries_grp() -> None:
    """CGM glucose entries (sgv, mbg, cal, etr)."""


@entries_grp.command("latest")
@click.option("--count", default=1, type=int, help="How many recent entries (default 1)")
@click.pass_context
def entries_latest(ctx: click.Context, count: int) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = entries_mod.latest(count=count, conn=conn)
    ctx.obj["session"]["last_fetched"]["entries"] = res
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        click.echo("  date                          sgv   direction")
        for e in res:
            click.echo(_format_entry_row(e))


@entries_grp.command("list")
@click.option("--count", default=50, type=int)
@click.option("--type", "type_", default=None, help="Filter by type (sgv/mbg/cal/etr)")
@click.option("--from", "date_gte", default=None, help="ISO date lower bound (e.g. 2025-01-01)")
@click.option("--to", "date_lte", default=None, help="ISO date upper bound")
@click.pass_context
def entries_list(ctx: click.Context, count: int, type_: str | None, date_gte: str | None, date_lte: str | None) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = entries_mod.list_entries(conn=conn, count=count, type_=type_, date_gte=date_gte, date_lte=date_lte)
    ctx.obj["session"]["last_fetched"]["entries"] = res
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        click.echo(f"  {len(res)} entries")
        for e in res:
            click.echo(_format_entry_row(e))


@entries_grp.command("get")
@click.argument("spec")
@click.pass_context
def entries_get(ctx: click.Context, spec: str) -> None:
    """Get an entry by id (24-hex) or filter (e.g. `sgv`)."""
    conn = _conn(ctx)
    _require_url(conn)
    res = entries_mod.get_entry(spec, conn=conn)
    _emit(ctx, res)


@entries_grp.command("add")
@click.option("--sgv", required=True, type=float, help="Glucose value (mg/dL)")
@click.option("--date-ms", default=None, type=int, help="Unix epoch milliseconds (default: now)")
@click.option("--direction", default="Flat",
              type=click.Choice(["DoubleUp", "SingleUp", "FortyFiveUp", "Flat",
                                  "FortyFiveDown", "SingleDown", "DoubleDown",
                                  "NONE", "NOT COMPUTABLE", "RATE OUT OF RANGE"]))
@click.option("--device", default="cli-anything-nightscout")
@click.option("--type", "type_", default="sgv", type=click.Choice(["sgv", "mbg", "cal", "etr"]))
@click.pass_context
def entries_add(
    ctx: click.Context, sgv: float, date_ms: int | None, direction: str, device: str, type_: str,
) -> None:
    """Upload a glucose reading (mutation; auto-saves session)."""
    conn = _conn(ctx)
    _require_url(conn)
    res = entries_mod.add_sgv(
        sgv=sgv, date_ms=date_ms, direction=direction, device=device, type_=type_, conn=conn,
    )
    _maybe_save_session(ctx, action="entries.add", detail=f"sgv={sgv}")
    _emit(ctx, res, human=f"posted sgv={sgv} direction={direction}")


@entries_grp.command("delete")
@click.argument("spec")
@click.confirmation_option(prompt="Really delete?")
@click.pass_context
def entries_delete(ctx: click.Context, spec: str) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = entries_mod.delete_entry(spec, conn=conn)
    _maybe_save_session(ctx, action="entries.delete", detail=spec)
    _emit(ctx, res, human=f"deleted {spec}")


@entries_grp.command("slice")
@click.option("--storage", default="entries")
@click.option("--field", default="dateString")
@click.option("--type", "type_", default="sgv")
@click.option("--prefix", required=True, help="Date prefix, e.g. 2025-01")
@click.option("--regex", default=".*", help="Tail regex (supports brace expansion)")
@click.pass_context
def entries_slice(ctx: click.Context, storage: str, field: str, type_: str, prefix: str, regex: str) -> None:
    """Prefix+regex slice query (advanced)."""
    conn = _conn(ctx)
    _require_url(conn)
    res = entries_mod.slice_query(
        storage=storage, field=field, type_=type_, prefix=prefix, regex=regex, conn=conn,
    )
    _emit(ctx, res, human=f"  {len(res)} entries match {prefix} {regex}")


# ─── treatments ────────────────────────────────────────────────────────────

@cli.group("treatments")
def treatments_grp() -> None:
    """Treatment events (boluses, meals, site changes, etc.)."""


@treatments_grp.command("latest")
@click.option("--count", default=1, type=int)
@click.pass_context
def treatments_latest(ctx: click.Context, count: int) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = treatments_mod.latest(count=count, conn=conn)
    ctx.obj["session"]["last_fetched"]["treatments"] = res
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        click.echo("  created_at                    type               detail")
        for t in res:
            click.echo(_format_treatment_row(t))


@treatments_grp.command("list")
@click.option("--count", default=50, type=int)
@click.option("--event-type", default=None)
@click.option("--from", "date_gte", default=None)
@click.option("--to", "date_lte", default=None)
@click.pass_context
def treatments_list(ctx: click.Context, count: int, event_type: str | None, date_gte: str | None, date_lte: str | None) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = treatments_mod.list_treatments(
        conn=conn, count=count, event_type=event_type, date_gte=date_gte, date_lte=date_lte,
    )
    ctx.obj["session"]["last_fetched"]["treatments"] = res
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        click.echo(f"  {len(res)} treatments")
        for t in res:
            click.echo(_format_treatment_row(t))


@treatments_grp.command("get")
@click.argument("spec")
@click.pass_context
def treatments_get(ctx: click.Context, spec: str) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = treatments_mod.get_treatment(spec, conn=conn)
    _emit(ctx, res)


@treatments_grp.command("add")
@click.option("--event-type", required=True, help="e.g. 'Meal Bolus', 'BG Check', 'Site Change'")
@click.option("--carbs", type=float, default=None)
@click.option("--insulin", type=float, default=None)
@click.option("--glucose", type=float, default=None)
@click.option("--glucose-type", default=None, type=click.Choice(["Finger", "Sensor", "Manual"]))
@click.option("--notes", default=None)
@click.option("--entered-by", default="cli-anything-nightscout")
@click.option("--created-at", default=None, help="ISO timestamp (default: now)")
@click.pass_context
def treatments_add(
    ctx: click.Context, event_type: str, carbs: float | None, insulin: float | None,
    glucose: float | None, glucose_type: str | None, notes: str | None, entered_by: str, created_at: str | None,
) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = treatments_mod.add_treatment(
        event_type=event_type, carbs=carbs, insulin=insulin, glucose=glucose,
        glucose_type=glucose_type, notes=notes, entered_by=entered_by, created_at=created_at, conn=conn,
    )
    _maybe_save_session(ctx, action="treatments.add", detail=event_type)
    _emit(ctx, res, human=f"posted treatment: {event_type}")


@treatments_grp.command("delete")
@click.argument("spec")
@click.confirmation_option(prompt="Really delete?")
@click.pass_context
def treatments_delete(ctx: click.Context, spec: str) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = treatments_mod.delete_treatment(spec, conn=conn)
    _maybe_save_session(ctx, action="treatments.delete", detail=spec)
    _emit(ctx, res, human=f"deleted {spec}")


# ─── profile ───────────────────────────────────────────────────────────────

@cli.group("profile")
def profile_grp() -> None:
    """Profile records (basal/ratio/sensitivity)."""


@profile_grp.command("active")
@click.pass_context
def profile_active(ctx: click.Context) -> None:
    """Return the active named profile body (basal / carbratio / sens / targets).

    This is what most callers actually want — the inner profile body, not the
    wrapper record. Use `profile current` for the full wrapper.
    """
    conn = _conn(ctx)
    _require_url(conn)
    body = profile_mod.current_store(conn=conn)
    if body is None:
        _emit(ctx, {}, human="no active profile found")
        return
    if _is_json(ctx):
        _emit(ctx, body)
    else:
        click.echo("Active profile:")
        if "basal" in body:
            click.echo(f"  basal slots:     {len(body['basal'])}")
        if "carbratio" in body:
            click.echo(f"  carbratio slots: {len(body['carbratio'])}")
        if "sens" in body:
            click.echo(f"  sens slots:      {len(body['sens'])}")
        if "target_low" in body:
            click.echo(f"  target_low slots: {len(body['target_low'])}")
        if "target_high" in body:
            click.echo(f"  target_high slots: {len(body['target_high'])}")
        if "dia" in body:
            click.echo(f"  DIA: {body['dia']}h")
        if "timezone" in body:
            click.echo(f"  timezone: {body['timezone']}")


@profile_grp.command("current")
@click.pass_context
def profile_current(ctx: click.Context) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = profile_mod.current(conn=conn)
    _emit(ctx, res or {})


@profile_grp.command("list")
@click.pass_context
def profile_list(ctx: click.Context) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = profile_mod.list_profiles(conn=conn)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        click.echo(f"  {len(res)} profile records")
        for p in res:
            click.echo(f"  {p.get('startDate', p.get('created_at','?'))}  default={p.get('defaultProfile','?')}")


# ─── devicestatus ──────────────────────────────────────────────────────────

@cli.group("devicestatus")
def devicestatus_grp() -> None:
    """Pump / CGM device status snapshots."""


@devicestatus_grp.command("latest")
@click.option("--count", default=1, type=int)
@click.pass_context
def devicestatus_latest(ctx: click.Context, count: int) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = ds_mod.latest(count=count, conn=conn)
    ctx.obj["session"]["last_fetched"]["devicestatus"] = res
    _emit(ctx, res)


@devicestatus_grp.command("list")
@click.option("--count", default=50, type=int)
@click.option("--from", "date_gte", default=None)
@click.pass_context
def devicestatus_list(ctx: click.Context, count: int, date_gte: str | None) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = ds_mod.list_devicestatus(conn=conn, count=count, date_gte=date_gte)
    _emit(ctx, res, human=f"  {len(res)} device status records")


@devicestatus_grp.command("delete")
@click.argument("spec")
@click.confirmation_option(prompt="Really delete?")
@click.pass_context
def devicestatus_delete(ctx: click.Context, spec: str) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = ds_mod.delete_devicestatus(spec, conn=conn)
    _maybe_save_session(ctx, action="devicestatus.delete", detail=spec)
    _emit(ctx, res, human=f"deleted {spec}")


# ─── food ──────────────────────────────────────────────────────────────────

@cli.group("food")
def food_grp() -> None:
    """Food database (API v3)."""


@food_grp.command("list")
@click.option("--limit", default=100, type=int)
@click.pass_context
def food_list(ctx: click.Context, limit: int) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = food_mod.list_food(conn=conn, limit=limit)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        click.echo(f"  {len(res)} food items")
        for f in res:
            click.echo(f"  {f.get('food','?'):<30} {f.get('carbs','?')}g carbs / {f.get('portion','?')} {f.get('unit','')}")


# ─── activity ──────────────────────────────────────────────────────────────

@cli.group("activity")
def activity_grp() -> None:
    """Activity / exercise records (API v3)."""


@activity_grp.command("latest")
@click.option("--count", default=1, type=int)
@click.pass_context
def activity_latest(ctx: click.Context, count: int) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = activity_mod.latest(count=count, conn=conn)
    _emit(ctx, res, human=f"  {len(res)} activity record(s)")


@activity_grp.command("list")
@click.option("--limit", default=50, type=int)
@click.option("--event-type", default=None,
                help="Filter by eventType (typically 'Exercise')")
@click.option("--from", "date_gte", default=None, help="ISO date lower bound")
@click.option("--to", "date_lte", default=None, help="ISO date upper bound")
@click.pass_context
def activity_list(ctx: click.Context, limit: int, event_type: str | None,
                    date_gte: str | None, date_lte: str | None) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = activity_mod.list_activity(
        conn=conn, limit=limit, event_type=event_type,
        date_gte=date_gte, date_lte=date_lte,
    )
    _emit(ctx, res, human=f"  {len(res)} activity record(s)")


@activity_grp.command("get")
@click.argument("identifier")
@click.pass_context
def activity_get(ctx: click.Context, identifier: str) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    _emit(ctx, activity_mod.get_activity(identifier, conn=conn))


@activity_grp.command("add")
@click.option("--event-type", default="Exercise",
                help="Activity event type (default 'Exercise')")
@click.option("--duration", type=float, default=None, help="Duration in minutes")
@click.option("--notes", default=None)
@click.option("--entered-by", default="cli-anything-nightscout")
@click.option("--created-at", default=None, help="ISO timestamp (default: now)")
@click.pass_context
def activity_add(ctx: click.Context, event_type: str, duration: float | None,
                   notes: str | None, entered_by: str,
                   created_at: str | None) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = activity_mod.add_activity(
        event_type=event_type, duration=duration, notes=notes,
        entered_by=entered_by, created_at=created_at, conn=conn,
    )
    _maybe_save_session(
        ctx, action="activity.add",
        detail=f"{event_type}{f' {duration}min' if duration else ''}",
    )
    _emit(ctx, res, human=f"added activity: {event_type}")


@activity_grp.command("delete")
@click.argument("identifier")
@click.confirmation_option(prompt="Really delete?")
@click.pass_context
def activity_delete(ctx: click.Context, identifier: str) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    res = activity_mod.delete_activity(identifier, conn=conn)
    _maybe_save_session(ctx, action="activity.delete", detail=identifier)
    _emit(ctx, res, human=f"deleted activity {identifier}")


# ─── reports ───────────────────────────────────────────────────────────────

@cli.group("report")
def report_grp() -> None:
    """Computed reports from glucose entries."""


def _is_mmol_units(units: str | None) -> bool:
    return (units or "").lower() in ("mmol", "mmol/l")


def _fmt_glucose(v: Any, mmol: bool) -> str:
    """Format a glucose value in the chosen units (uses *_mmol field if available)."""
    if v is None:
        return "—"
    return f"{v} {'mmol/L' if mmol else 'mg/dL'}"


@report_grp.command("tir")
@click.option("--count", default=288, type=int, help="Entries to consider (288 ≈ 24h at 5min cadence)")
@click.option("--low", default=None, type=float,
                help="Low threshold (default: 70 mg/dL or 3.9 mmol/L)")
@click.option("--high", default=None, type=float,
                help="High threshold (default: 180 mg/dL or 10.0 mmol/L)")
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]),
                help="Override session units for this report")
@click.option("--from", "date_gte", default=None, help="ISO start (overrides --count)")
@click.option("--to", "date_lte", default=None)
@click.pass_context
def report_tir(ctx: click.Context, count: int, low: float | None, high: float | None,
                 units_flag: str | None, date_gte: str | None, date_lte: str | None) -> None:
    """Time-In-Range report. Honors session units (mg/dL or mmol/L)."""
    conn = _conn(ctx)
    _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    if date_gte or date_lte:
        data = entries_mod.list_entries(conn=conn, count=10000, type_="sgv", date_gte=date_gte, date_lte=date_lte)
    else:
        data = entries_mod.latest(count=count, conn=conn)
    res = report_mod.time_in_range(data, low=low, high=high, units=units)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        u = "mmol/L" if _is_mmol_units(units) else "mg/dL"
        lo = res["low_threshold"]
        hi = res["high_threshold"]
        click.echo(f"  readings: {res['total_readings']}")
        click.echo(f"  TIR ({lo}–{hi} {u}): {res['tir_pct']}%   ({res.get('in_range_count','?')} readings)")
        click.echo(f"  TBR (<{lo} {u}):           {res['tbr_pct']}%   ({res.get('below_count','?')} readings)")
        click.echo(f"  TAR (>{hi} {u}):           {res['tar_pct']}%   ({res.get('above_count','?')} readings)")


@report_grp.command("summary")
@click.option("--count", default=288, type=int)
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]),
                help="Override session units")
@click.option("--from", "date_gte", default=None)
@click.option("--to", "date_lte", default=None)
@click.pass_context
def report_summary(ctx: click.Context, count: int, units_flag: str | None,
                     date_gte: str | None, date_lte: str | None) -> None:
    conn = _conn(ctx)
    _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)
    if date_gte or date_lte:
        data = entries_mod.list_entries(conn=conn, count=10000, type_="sgv", date_gte=date_gte, date_lte=date_lte)
    else:
        data = entries_mod.latest(count=count, conn=conn)
    res = report_mod.summary(data, units=units)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        mean = res.get("mean_mmol") if mmol else res.get("mean_mgdl")
        stdev = res.get("stdev_mmol") if mmol else res.get("stdev_mgdl")
        mn = res.get("min_mmol") if mmol else res.get("min_mgdl")
        mx = res.get("max_mmol") if mmol else res.get("max_mgdl")
        click.echo(f"  count: {res['count']}")
        click.echo(f"  mean:  {_fmt_glucose(mean, mmol)}")
        click.echo(f"  stdev: {_fmt_glucose(stdev, mmol)}")
        click.echo(f"  min:   {_fmt_glucose(mn, mmol)}")
        click.echo(f"  max:   {_fmt_glucose(mx, mmol)}")
        click.echo(f"  CV:    {res['cv_pct']}%")
        click.echo(f"  GMI:   {res['gmi_pct']}% (est. A1C)")


@report_grp.command("gmi")
@click.option("--count", default=288, type=int)
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.pass_context
def report_gmi(ctx: click.Context, count: int, units_flag: str | None) -> None:
    """Glucose Management Indicator (estimated A1C)."""
    conn = _conn(ctx)
    _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)
    data = entries_mod.latest(count=count, conn=conn)
    res = report_mod.gmi(data, units=units)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        mean = res.get("mean_mmol") if mmol else res.get("mean_mgdl")
        click.echo(f"  count: {res['count']}")
        click.echo(f"  mean:  {_fmt_glucose(mean, mmol)}")
        click.echo(f"  GMI:   {res['gmi_pct']}%")


@report_grp.command("daily")
@click.option("--count", default=2016, type=int, help="Default 2016 ≈ 7 days at 5min cadence")
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.option("--from", "date_gte", default=None)
@click.option("--to", "date_lte", default=None)
@click.pass_context
def report_daily(ctx: click.Context, count: int, units_flag: str | None,
                   date_gte: str | None, date_lte: str | None) -> None:
    """Per-day glucose stats."""
    conn = _conn(ctx)
    _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)
    if date_gte or date_lte:
        data = entries_mod.list_entries(conn=conn, count=10000, type_="sgv", date_gte=date_gte, date_lte=date_lte)
    else:
        data = entries_mod.latest(count=count, conn=conn)
    res = report_mod.daily(data, units=units)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        u = "mmol" if mmol else "mg/dL"
        click.echo(f"  date         count   mean({u})   min   max   TIR%")
        for r in res:
            mean = r.get("mean_mmol") if mmol else r.get("mean_mgdl")
            mn = r.get("min_mmol") if mmol else r.get("min_mgdl")
            mx = r.get("max_mmol") if mmol else r.get("max_mgdl")
            click.echo(
                f"  {r['date']}   {r['count']:>5}   {str(mean):>5}     {str(mn):>5}  {str(mx):>5}   {r['tir_pct']:>5}"
            )


@report_grp.command("agp")
@click.option("--days", default=14, type=int, help="Window size in days (default 14)")
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.option("--from", "date_gte", default=None,
                help="ISO date lower bound (overrides --days)")
@click.option("--to", "date_lte", default=None)
@click.pass_context
def report_agp(ctx: click.Context, days: int, units_flag: str | None,
                 date_gte: str | None, date_lte: str | None) -> None:
    """Ambulatory Glucose Profile — percentiles per hour-of-day.

    Surfaces dawn-phenomenon, mealtime spikes, and overnight stability across
    the window. Standard clinic-report shape.
    """
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx)
    _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)

    if not date_gte and not date_lte:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        date_gte = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        date_lte = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = entries_mod.list_entries(
        conn=conn, count=100000, type_="sgv",
        date_gte=date_gte, date_lte=date_lte,
    )
    # Nightscout stores sgv in mg/dL; honor that explicitly.
    rows = report_mod.hourly_pattern(
        data, units=units, input_units="mg/dl",
    )
    if _is_json(ctx):
        _emit(ctx, {"window_days": days, "rows": rows})
    else:
        u = "mmol/L" if mmol else "mg/dL"
        click.echo(f"  AGP — {sum(r['count'] for r in rows)} readings over {days} days")
        click.echo(f"  hour  count   p10    p25   p50   p75   p90    mean   TIR%")
        for r in rows:
            if r["count"] == 0:
                click.echo(f"  {r['hour']:>2d}h     0    —     —     —     —     —      —       —")
                continue
            if mmol:
                p10 = r["p10_mmol"]; p25 = r["p25_mmol"]; p50 = r["p50_mmol"]
                p75 = r["p75_mmol"]; p90 = r["p90_mmol"]; mean = r["mean_mmol"]
            else:
                p10 = r["p10_mgdl"]; p25 = r["p25_mgdl"]; p50 = r["p50_mgdl"]
                p75 = r["p75_mgdl"]; p90 = r["p90_mgdl"]; mean = r["mean_mgdl"]
            click.echo(
                f"  {r['hour']:>2d}h  {r['count']:>4d}   {p10:>4}  {p25:>4}  {p50:>4}  {p75:>4}  {p90:>4}   {mean:>5}   {r['tir_pct']:>5}"
            )
        click.echo(f"\n  values in {u}; p50 = median, IQR = p25–p75")


@report_grp.command("hypos")
@click.option("--days", default=14, type=int, help="Window size in days (default 14)")
@click.option("--threshold", default=None, type=float,
                help="Hypo threshold (default: 70 mg/dL or 3.9 mmol/L)")
@click.option("--min-duration", default=15, type=int,
                help="Minimum event duration in minutes (default 15)")
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.option("--from", "date_gte", default=None,
                help="ISO date lower bound (overrides --days)")
@click.option("--to", "date_lte", default=None)
@click.pass_context
def report_hypos(ctx: click.Context, days: int, threshold: float | None,
                   min_duration: int, units_flag: str | None,
                   date_gte: str | None, date_lte: str | None) -> None:
    """Distinct hypoglycemic events (sustained dips below threshold).

    Different from `report tir`'s TBR% — this counts EVENTS (≥ min_duration
    minutes below threshold) rather than raw readings, per Battelino 2019.
    Each event reports duration, min glucose, and Level 1 / Level 2 class.
    """
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx)
    _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)

    if not date_gte and not date_lte:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        date_gte = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        date_lte = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = entries_mod.list_entries(
        conn=conn, count=100000, type_="sgv",
        date_gte=date_gte, date_lte=date_lte,
    )
    evts = report_mod.hypo_events(
        data, threshold=threshold, min_duration_min=min_duration,
        units=units, input_units="mg/dl",
    )
    if _is_json(ctx):
        _emit(ctx, {"window_days": days, "events": evts})
    else:
        u = "mmol/L" if mmol else "mg/dL"
        click.echo(f"  {len(evts)} hypo event(s) ≥{min_duration}min over {days} days")
        if not evts:
            return
        l1 = sum(1 for e in evts if e["level"] == "level_1")
        l2 = sum(1 for e in evts if e["level"] == "level_2")
        total_min = sum(e["duration_min"] for e in evts)
        longest = max(evts, key=lambda e: e["duration_min"])
        click.echo(f"  Level 1: {l1}   Level 2: {l2}   Total time hypo: {total_min:.0f} min")
        click.echo(f"  Longest event: {longest['duration_min']:.0f}min "
                    f"(min {longest.get('min_mmol' if mmol else 'min_mgdl')} {u})")
        click.echo("")
        click.echo(f"  {'when':<22s} {'dur':>6s}  {'min':>5s}  level")
        for e in evts:
            mn = e.get("min_mmol") if mmol else e.get("min_mgdl")
            click.echo(f"  {e['start'][:19]:<22s} {e['duration_min']:>4.0f}m  {mn:>5}  {e['level']}")


# ─── session ───────────────────────────────────────────────────────────────

@cli.group("session")
def session_grp() -> None:
    """Session state (local cache + history)."""


@session_grp.command("info")
@click.pass_context
def session_info(ctx: click.Context) -> None:
    sess = ctx.obj["session"]
    payload = {
        "name": sess.get("name"),
        "server_url": sess.get("server_url"),
        "modified": sess.get("modified", False),
        "history_count": len(sess.get("history", [])),
        "cached_entries": len(sess.get("last_fetched", {}).get("entries", [])),
        "cached_treatments": len(sess.get("last_fetched", {}).get("treatments", [])),
        "session_path": str(ctx.obj.get("session_path") or project.DEFAULT_SESSION_FILE),
    }
    _emit(ctx, payload)


@session_grp.command("save")
@click.pass_context
def session_save(ctx: click.Context) -> None:
    p = project.save_session(ctx.obj["session"], ctx.obj.get("session_path"))
    _emit(ctx, {"saved": True, "path": str(p)}, human=f"session saved → {p}")


@session_grp.command("load")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def session_load(ctx: click.Context, path: str) -> None:
    ctx.obj["session"] = project.load_session(path)
    ctx.obj["session_path"] = path
    _emit(ctx, {"loaded": True, "path": path}, human=f"session loaded ← {path}")


@session_grp.command("clear")
@click.confirmation_option(prompt="Reset session state?")
@click.pass_context
def session_clear(ctx: click.Context) -> None:
    ctx.obj["session"] = project.new_session()
    if not ctx.obj.get("dry_run"):
        project.save_session(ctx.obj["session"], ctx.obj.get("session_path"))
    _emit(ctx, {"cleared": True}, human="session cleared")


if __name__ == "__main__":
    main()
