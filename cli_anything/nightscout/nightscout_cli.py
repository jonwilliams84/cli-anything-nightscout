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
    excursions as excursions_mod,
    food as food_mod,
    notifications as notifications_mod,
    profile as profile_mod,
    project,
    properties as properties_mod,
    report as report_mod,
    sensors as sensors_mod,
    status as status_mod,
    treatments as treatments_mod,
    v3 as v3_mod,
    watch as watch_mod,
)
from cli_anything.nightscout.utils import nightscout_backend as backend
from cli_anything.nightscout.utils.repl_skin import ReplSkin


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
VERSION = "2.1.0"


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
    """Auto-save session after a mutation unless --dry-run is set.

    Note: with v2.1.0's network-safe --dry-run semantics, dry-run guards run
    *before* reaching this helper, so the inner check here is belt-and-braces.
    """
    if ctx.obj.get("dry_run"):
        return
    sess = ctx.obj.get("session")
    if sess is None:
        return
    project.record_history(sess, action, detail)
    sess["modified"] = True
    project.save_session(sess, ctx.obj.get("session_path"))


def _dry_run_block(
    ctx: click.Context,
    verb_path: str,
    **extras: Any,
) -> bool:
    """If --dry-run is set, emit a description and return True.

    Mutating CLI commands MUST call this before any network call. When set,
    no HTTP request is made; instead the CLI emits
    ``{"dry_run": true, "would": "<verb> <path>", ...extras}`` and the caller
    short-circuits.
    """
    if ctx.obj.get("dry_run"):
        payload: dict[str, Any] = {"dry_run": True, "would": verb_path}
        payload.update(extras)
        _emit(ctx, payload, human=f"[dry-run] would {verb_path}")
        return True
    return False


_OBJECTID_RE = __import__("re").compile(r"^[0-9a-fA-F]{24}$")


def _is_object_id(spec: str) -> bool:
    """True if ``spec`` looks like a MongoDB ObjectId (24 hex chars)."""
    return bool(spec and _OBJECTID_RE.match(spec))


def _warn_truncation(data: Any, *, limit: int, ctx: click.Context) -> None:
    """Warn on stderr if ``data`` looks like it was capped at ``limit``.

    Several report commands hardcode a large ``count=`` when the caller
    supplies ``--from``/``--to``. If the server returns exactly that many
    records, the window is likely *truncated* — there are probably more
    readings outside the slice we asked for. Surface that so silent
    truncation doesn't skew the report.
    """
    if not isinstance(data, list):
        return
    n = len(data)
    if n >= limit:
        click.echo(
            f"  ⚠ result hit count limit ({n}/{limit}); window may be "
            f"truncated. Narrow --from/--to or paginate to be sure.",
            err=True,
        )


def _default_tz_name() -> str:
    """Best-effort local IANA tz name for use as the report --tz default.

    Falls back to UTC if the local zone is opaque (e.g. unset $TZ in a
    bare container). When the resolved name can't be matched to an IANA zone
    the report helpers will fall back to UTC internally, so this is purely
    informational for help text + the JSON `tz_used` field.
    """
    try:
        from datetime import datetime, timezone as _tz
        from time import tzname
        local = datetime.now().astimezone().tzinfo
        if local is not None:
            name = getattr(local, "key", None) or str(local)
            return name
        return tzname[0] if tzname else "UTC"
    except Exception:
        return "UTC"


def _confirm(ctx: click.Context, prompt: str, *, yes: bool) -> bool:
    """Gate a destructive action.

    Returns True when the caller may proceed. ``--yes`` bypasses the prompt
    (required for non-interactive / scripted use). Without ``--yes`` we use
    ``click.confirm`` which fails closed on non-interactive stdin.
    """
    if yes:
        return True
    try:
        return click.confirm(prompt, default=False)
    except click.exceptions.Abort:
        return False


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
@click.option("--dry-run", is_flag=True, default=False,
              help="Describe mutating requests without sending them. No "
                   "network call is made; the CLI emits "
                   "{'dry_run': true, 'would': '<verb> <path>'}.")
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
        "status": "Server status / version / versions / lastModified / verifyauth",
        "entries": "Glucose entries: latest, current, list, get, add, delete, slice, count, times, normalize",
        "treatments": "Treatment events: latest, list, get, add, update, delete, bg-check",
        "profile": "Profile records: current, active, list, get-named, setting-at, schedule, create, update, delete",
        "devicestatus": "Device status: latest, list, add, delete",
        "food": "Food database: list, quickpicks, regular, add, update, delete",
        "activity": "Activity records: latest, list, get, add, delete",
        "sensors": "CGM sensor sessions: sessions",
        "properties": "Derived state (IOB/COB/bgnow/loop): get",
        "notifications": "Alarms: ack, admin",
        "report": "Computed reports: tir, summary, daily, gmi, agp, hypos, mage, risk, by-weekday, excursions, sensor-life, iob-cob",
        "v3": "Generic v3 CRUD: list, get, create, update, patch, delete, search, history",
        "watch": "Real-time entries/treatments stream (needs '.[watch]')",
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
    if _dry_run_block(ctx, "POST /entries.json",
                       payload={"sgv": sgv, "direction": direction,
                                "device": device, "type": type_,
                                "date_ms": date_ms}):
        return
    res = entries_mod.add_sgv(
        sgv=sgv, date_ms=date_ms, direction=direction, device=device, type_=type_, conn=conn,
    )
    _maybe_save_session(ctx, action="entries.add", detail=f"sgv={sgv}")
    _emit(ctx, res, human=f"posted sgv={sgv} direction={direction}")


@entries_grp.command("delete")
@click.argument("spec")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the interactive confirmation (required for scripted use).")
@click.pass_context
def entries_delete(ctx: click.Context, spec: str, yes: bool) -> None:
    """Delete a single entry by its MongoDB ObjectId (24-hex).

    For mass-delete by type (e.g. "delete every sgv reading"), use
    ``entries delete-by-type`` — it has explicit safety rails.
    """
    if not _is_object_id(spec):
        raise click.ClickException(
            f"entries delete requires a 24-hex ObjectId; got {spec!r}. "
            f"To mass-delete by type (sgv/mbg/cal/etr) use "
            f"`entries delete-by-type <type> --before <iso> --apply`."
        )
    if not _confirm(ctx, f"Delete entry {spec}?", yes=yes):
        raise click.ClickException("aborted")
    conn = _conn(ctx)
    _require_url(conn)
    if _dry_run_block(ctx, f"DELETE /entries/{spec}"):
        return
    res = entries_mod.delete_entry(spec, conn=conn)
    _maybe_save_session(ctx, action="entries.delete", detail=spec)
    _emit(ctx, res, human=f"deleted {spec}")


@entries_grp.command("delete-by-type")
@click.argument("type_filter", type=click.Choice(["sgv", "mbg", "cal", "etr"]))
@click.option("--before", "before", default=None,
              help="ISO datetime upper bound (delete only entries before this).")
@click.option("--after", "after", default=None,
              help="ISO datetime lower bound (delete only entries after this).")
@click.option("--apply", "apply_", is_flag=True, default=False,
              help="Actually delete. Without this the command lists what would be removed.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the interactive confirmation (required for scripted use).")
@click.pass_context
def entries_delete_by_type(
    ctx: click.Context,
    type_filter: str,
    before: str | None,
    after: str | None,
    apply_: bool,
    yes: bool,
) -> None:
    """Mass-delete every entry matching <type_filter>.

    This is the explicit, gated form of the historic "delete spec" footgun:
    Nightscout's DELETE /api/v1/entries/sgv removes every SGV ever stored.
    Default is preview-only (lists matched IDs). Pass --apply to commit.

    Requires either --before or --after (refuses to delete the entire
    collection without a time bound).
    """
    if not before and not after:
        raise click.ClickException(
            "refusing to delete every entry of type "
            f"{type_filter!r} without --before or --after. Pass one (or both)."
        )
    conn = _conn(ctx)
    _require_url(conn)
    # Preview: list matching entries first.
    matches = entries_mod.list_entries(
        conn=conn, count=100000, type_=type_filter,
        date_gte=after, date_lte=before,
    )
    ids = [m.get("_id") for m in matches if isinstance(m, dict) and m.get("_id")]
    if not apply_:
        _emit(ctx, {"dry_run": True, "matched": len(ids), "ids": ids,
                     "type": type_filter, "before": before, "after": after},
              human=f"[preview] {len(ids)} {type_filter} entries match; "
                    f"pass --apply to delete")
        return
    if not _confirm(ctx,
                     f"Delete {len(ids)} entries of type {type_filter!r}?",
                     yes=yes):
        raise click.ClickException("aborted")
    if _dry_run_block(ctx,
                       f"DELETE /entries/{type_filter} (bounded by "
                       f"after={after!r} before={before!r}; matched {len(ids)})"):
        return
    deleted = []
    errors: list[dict] = []
    for _id in ids:
        try:
            entries_mod.delete_entry(_id, conn=conn)
            deleted.append(_id)
        except Exception as exc:
            errors.append({"id": _id, "error": str(exc)[:200]})
    _maybe_save_session(
        ctx, action="entries.delete_by_type",
        detail=f"type={type_filter} count={len(deleted)}",
    )
    _emit(ctx, {"matched": len(ids), "deleted": len(deleted),
                 "errors": errors,
                 "type": type_filter, "before": before, "after": after},
          human=f"deleted {len(deleted)}/{len(ids)} {type_filter} entries")


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
    if _dry_run_block(ctx, "POST /treatments.json",
                       payload={"eventType": event_type, "carbs": carbs,
                                "insulin": insulin, "glucose": glucose,
                                "glucoseType": glucose_type, "notes": notes}):
        return
    res = treatments_mod.add_treatment(
        event_type=event_type, carbs=carbs, insulin=insulin, glucose=glucose,
        glucose_type=glucose_type, notes=notes, entered_by=entered_by, created_at=created_at, conn=conn,
    )
    _maybe_save_session(ctx, action="treatments.add", detail=event_type)
    _emit(ctx, res, human=f"posted treatment: {event_type}")


@treatments_grp.command("delete")
@click.argument("spec")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the interactive confirmation (required for scripted use).")
@click.pass_context
def treatments_delete(ctx: click.Context, spec: str, yes: bool) -> None:
    if not _confirm(ctx, f"Delete treatment {spec}?", yes=yes):
        raise click.ClickException("aborted")
    conn = _conn(ctx)
    _require_url(conn)
    if _dry_run_block(ctx, f"DELETE /treatments/{spec}"):
        return
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
@click.option("--yes", is_flag=True, default=False,
              help="Skip the interactive confirmation (required for scripted use).")
@click.pass_context
def devicestatus_delete(ctx: click.Context, spec: str, yes: bool) -> None:
    if not _confirm(ctx, f"Delete device status {spec}?", yes=yes):
        raise click.ClickException("aborted")
    conn = _conn(ctx)
    _require_url(conn)
    if _dry_run_block(ctx, f"DELETE /devicestatus/{spec}"):
        return
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
    if _dry_run_block(ctx, "POST /v3/activity",
                       payload={"eventType": event_type, "duration": duration,
                                "notes": notes}):
        return
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
@click.option("--yes", is_flag=True, default=False,
              help="Skip the interactive confirmation (required for scripted use).")
@click.pass_context
def activity_delete(ctx: click.Context, identifier: str, yes: bool) -> None:
    if not _confirm(ctx, f"Delete activity {identifier}?", yes=yes):
        raise click.ClickException("aborted")
    conn = _conn(ctx)
    _require_url(conn)
    if _dry_run_block(ctx, f"DELETE /v3/activity/{identifier}"):
        return
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
        _warn_truncation(data, limit=10000, ctx=ctx)
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
        _warn_truncation(data, limit=10000, ctx=ctx)
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
@click.option("--tz", "tz_name", default=None,
              help="Day-boundary timezone (default: local system tz). "
                   "IANA name, e.g. 'Europe/London' or 'UTC'.")
@click.pass_context
def report_daily(ctx: click.Context, count: int, units_flag: str | None,
                   date_gte: str | None, date_lte: str | None,
                   tz_name: str | None) -> None:
    """Per-day glucose stats."""
    conn = _conn(ctx)
    _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)
    tz = tz_name or _default_tz_name()
    if date_gte or date_lte:
        data = entries_mod.list_entries(conn=conn, count=10000, type_="sgv", date_gte=date_gte, date_lte=date_lte)
        _warn_truncation(data, limit=10000, ctx=ctx)
    else:
        data = entries_mod.latest(count=count, conn=conn)
    res = report_mod.daily(data, units=units, tz=tz)
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
@click.option("--tz", "tz_name", default=None,
              help="Hour-of-day timezone (default: local system tz). "
                   "IANA name, e.g. 'Europe/London' or 'UTC'.")
@click.pass_context
def report_agp(ctx: click.Context, days: int, units_flag: str | None,
                 date_gte: str | None, date_lte: str | None,
                 tz_name: str | None) -> None:
    """Ambulatory Glucose Profile — percentiles per hour-of-day.

    Surfaces dawn-phenomenon, mealtime spikes, and overnight stability across
    the window. Standard clinic-report shape.
    """
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx)
    _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)
    tz = tz_name or _default_tz_name()

    if not date_gte and not date_lte:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        date_gte = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        date_lte = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = entries_mod.list_entries(
        conn=conn, count=100000, type_="sgv",
        date_gte=date_gte, date_lte=date_lte,
    )
    _warn_truncation(data, limit=100000, ctx=ctx)
    # Nightscout stores sgv in mg/dL; honor that explicitly.
    rows = report_mod.hourly_pattern(
        data, units=units, input_units="mg/dl", tz=tz,
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
    _warn_truncation(data, limit=100000, ctx=ctx)
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


# ─── report: NEW v2 — MAGE / risk / day-of-week ────────────────────────────

@report_grp.command("mage")
@click.option("--days", default=14, type=int)
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.pass_context
def report_mage(ctx: click.Context, days: int, units_flag: str | None) -> None:
    """Mean Amplitude of Glycemic Excursions (Service 1970)."""
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx); _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)
    end = datetime.now(timezone.utc); start = end - timedelta(days=days)
    data = entries_mod.list_entries(
        conn=conn, count=100000, type_="sgv",
        date_gte=start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        date_lte=end.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    res = report_mod.mage(data, units=units, input_units="mg/dl")
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        u = "mmol/L" if mmol else "mg/dL"
        v = res.get("mage_mmol") if mmol else res.get("mage_mgdl")
        click.echo(f"  MAGE ({days}d): {v if v is not None else '—'} {u}")
        click.echo(f"  Excursions counted: {res.get('count_excursions', 0)}")


@report_grp.command("risk")
@click.option("--days", default=14, type=int)
@click.pass_context
def report_risk(ctx: click.Context, days: int) -> None:
    """Kovatchev LBGI / HBGI risk indices."""
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx); _require_url(conn)
    end = datetime.now(timezone.utc); start = end - timedelta(days=days)
    data = entries_mod.list_entries(
        conn=conn, count=100000, type_="sgv",
        date_gte=start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        date_lte=end.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    res = report_mod.risk_indices(data, units="mg/dl")
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        click.echo(f"  LBGI: {res['lbgi']:.2f}  ({res['lbgi_risk']})  — hypoglycemia risk")
        click.echo(f"  HBGI: {res['hbgi']:.2f}  ({res['hbgi_risk']})  — hyperglycemia risk")
        click.echo(f"  Readings: {res['count']}")


@report_grp.command("by-weekday")
@click.option("--days", default=14, type=int)
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.option("--tz", "tz_name", default=None,
              help="Weekday-boundary timezone (default: local system tz).")
@click.pass_context
def report_by_weekday(ctx: click.Context, days: int, units_flag: str | None,
                        tz_name: str | None) -> None:
    """Per-weekday TIR / mean / TBR / TAR."""
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx); _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)
    tz = tz_name or _default_tz_name()
    end = datetime.now(timezone.utc); start = end - timedelta(days=days)
    data = entries_mod.list_entries(
        conn=conn, count=100000, type_="sgv",
        date_gte=start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        date_lte=end.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    rows = report_mod.day_of_week(data, units=units, input_units="mg/dl", tz=tz)
    if _is_json(ctx):
        _emit(ctx, rows)
    else:
        click.echo(f"  day   #     mean   TIR%   TBR%   TAR%")
        for r in rows:
            mean = r.get("mean_mmol") if mmol else r.get("mean_mgdl")
            click.echo(f"  {r['weekday']:<5s} {r['count']:>4d}  {str(mean):>5}  {r['tir_pct']:>5.1f}  {r['tbr_pct']:>5.2f}  {r['tar_pct']:>5.1f}")


# ─── report: excursions ────────────────────────────────────────────────────

@report_grp.command("excursions")
@click.option("--days", default=14, type=int)
@click.option("--window-min", default=120, type=int)
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.pass_context
def report_excursions(ctx: click.Context, days: int, window_min: int,
                        units_flag: str | None) -> None:
    """Post-meal glucose responses paired with the meal/bolus that drove them."""
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx); _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    mmol = _is_mmol_units(units)
    end = datetime.now(timezone.utc); start = end - timedelta(days=days)
    iso_s = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_e = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sgvs = entries_mod.list_entries(conn=conn, count=100000, type_="sgv",
                                       date_gte=iso_s, date_lte=iso_e)
    txs = treatments_mod.list_treatments(conn=conn, count=10000,
                                            date_gte=iso_s, date_lte=iso_e)
    rows = excursions_mod.postprandial_responses(
        sgvs, txs, window_min=window_min, units=units, input_units="mg/dl",
    )
    if _is_json(ctx):
        _emit(ctx, rows)
    else:
        u = "mmol/L" if mmol else "mg/dL"
        click.echo(f"  {len(rows)} qualifying meals over {days}d, {window_min}min window")
        click.echo(f"  {'when':<19s} {'carbs':>5s} {'ins':>5s} {'base':>5s} {'peak':>5s} {'Δ':>5s} {'ttp':>5s} {'ICR':>5s}")
        for r in rows[:50]:
            base = r.get("baseline_mmol") if mmol else r.get("baseline_mgdl")
            peak = r.get("peak_mmol") if mmol else r.get("peak_mgdl")
            delta = r.get("delta_mmol") if mmol else r.get("delta_mgdl")
            icr = r.get("ICR_effective_g_per_u")
            click.echo(f"  {r['created_at'][:19]:<19s} {str(r.get('carbs','?')):>5} {str(r.get('insulin','?')):>5} {str(base):>5} {str(peak):>5} {str(delta):>5} {str(r.get('time_to_peak_min','?')):>5} {str(icr) if icr is not None else '—':>5}")


@report_grp.command("excursions-by-hour")
@click.option("--days", default=14, type=int)
@click.option("--units", "units_flag", default=None,
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.option("--tz", "tz_name", default=None,
              help="Hour-of-day timezone (default: local system tz).")
@click.pass_context
def report_excursions_by_hour(ctx: click.Context, days: int,
                                 units_flag: str | None,
                                 tz_name: str | None) -> None:
    """Mean post-meal Δglucose / ICR by hour-of-day."""
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx); _require_url(conn)
    units = units_flag or conn.get("units", "mg/dl")
    tz = tz_name or _default_tz_name()
    end = datetime.now(timezone.utc); start = end - timedelta(days=days)
    iso_s = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_e = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sgvs = entries_mod.list_entries(conn=conn, count=100000, type_="sgv",
                                       date_gte=iso_s, date_lte=iso_e)
    txs = treatments_mod.list_treatments(conn=conn, count=10000,
                                            date_gte=iso_s, date_lte=iso_e)
    rows = excursions_mod.postprandial_responses(
        sgvs, txs, units=units, input_units="mg/dl")
    summ = excursions_mod.excursion_summary(rows, bucket="hour", tz=tz)
    if _is_json(ctx):
        _emit(ctx, summ)
    else:
        mmol_mode = _is_mmol_units(units)
        click.echo(f"  hour  meals  mean_baseline  mean_peak  mean_Δ  mean_ICR(g/U)")
        for r in summ:
            if mmol_mode:
                base = r.get("mean_baseline_mmol", r.get("mean_baseline_mgdl"))
                peak = r.get("mean_peak_mmol", r.get("mean_peak_mgdl"))
                delta = r.get("mean_delta_mmol", r.get("mean_delta_mgdl"))
            else:
                base = r.get("mean_baseline_mgdl")
                peak = r.get("mean_peak_mgdl")
                delta = r.get("mean_delta_mgdl")
            icr = r.get("mean_ICR_effective_g_per_u")
            click.echo(f"  {r.get('hour','?'):>3}h  {r.get('count','?'):>5} {str(base) if base is not None else '—':>13} {str(peak) if peak is not None else '—':>10} {str(delta) if delta is not None else '—':>7} {str(icr) if icr is not None else '—':>13}")


# ─── profile: schedule snapshot ────────────────────────────────────────────

@profile_grp.command("schedule")
@click.option("--at", "at_time", default=None,
                help="HH:MM time of day (default: now in local time)")
@click.pass_context
def profile_schedule(ctx: click.Context, at_time: str | None) -> None:
    """Show active basal / carbratio / sens / target at a given time."""
    from datetime import datetime
    conn = _conn(ctx); _require_url(conn)
    if at_time is None:
        at_time = datetime.now().strftime("%H:%M")
    store = profile_mod.current_store(conn=conn)
    if not store:
        _emit(ctx, {}, human="no active profile found"); return
    snap = profile_mod.schedule_snapshot(store, at_time)
    if _is_json(ctx):
        _emit(ctx, {"at": at_time, **snap})
    else:
        click.echo(f"Active profile at {at_time}:")
        for k, v in snap.items():
            click.echo(f"  {k:<13s} {v if v is not None else '—'}")


# ─── sensors ───────────────────────────────────────────────────────────────

@cli.group("sensors")
def sensors_grp() -> None:
    """CGM sensor session detection."""


@sensors_grp.command("sessions")
@click.option("--days", default=30, type=int)
@click.option("--with-stats", is_flag=True, default=False,
                help="Include entry counts per session")
@click.pass_context
def sensors_sessions(ctx: click.Context, days: int, with_stats: bool) -> None:
    """List sensor sessions in the recent window."""
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx); _require_url(conn)
    end = datetime.now(timezone.utc); start = end - timedelta(days=days)
    iso_s = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_e = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    txs = treatments_mod.list_treatments(conn=conn, count=10000,
                                            date_gte=iso_s, date_lte=iso_e)
    sgvs = None
    if with_stats:
        sgvs = entries_mod.list_entries(conn=conn, count=100000, type_="sgv",
                                            date_gte=iso_s, date_lte=iso_e)
    sessions = sensors_mod.sensor_sessions(txs, entries=sgvs)
    if _is_json(ctx):
        _emit(ctx, sessions)
    else:
        click.echo(f"  {len(sessions)} sensor session(s) over {days}d")
        for s in sessions:
            end_disp = s.get("end") or "(ongoing)"
            click.echo(f"  #{s['session_index']:>2}  {s['start'][:19]} → {end_disp[:19]:<19s}  {s['duration_days']:>5.1f}d  {s['marker_event_type']}"
                      + (f"  {s.get('entries_count',0)} entries" if with_stats else ""))


# ─── v3 generic CRUD ───────────────────────────────────────────────────────

@cli.group("v3")
def v3_grp() -> None:
    """Generic CRUD for any v3 collection."""


@v3_grp.command("list")
@click.argument("collection")
@click.option("--limit", default=100, type=int)
@click.option("--sort", "sort", default=None,
              help="Sort spec, e.g. '-srvModified' (descending). Passed to v3 API.")
@click.option("--filter", "filters", multiple=True,
              help="Field-level filter, e.g. 'eventType$eq=Meal Bolus'. "
                   "Repeatable; see v3 docs for operator syntax.")
@click.pass_context
def v3_list_cmd(ctx: click.Context, collection: str, limit: int,
                sort: str | None, filters: tuple[str, ...]) -> None:
    """List documents in a v3 collection.

    --filter values use the ``field$op=value`` form, e.g.
    ``date$gte=1700000000`` or ``eventType$eq=Meal Bolus``. Repeat the flag
    for multiple filters (server-side AND).
    """
    conn = _conn(ctx); _require_url(conn)
    filter_dict: dict[str, Any] = {}
    for raw in filters:
        if "=" not in raw:
            raise click.ClickException(
                f"--filter expects field$op=value, got {raw!r}"
            )
        k, v = raw.split("=", 1)
        filter_dict[k.strip()] = v.strip()
    res = v3_mod.v3_list(collection, conn=conn, limit=limit,
                          sort=sort, filter=filter_dict or None)
    _emit(ctx, res, human=f"  {len(res)} record(s) in '{collection}'")


@v3_grp.command("get")
@click.argument("collection")
@click.argument("identifier")
@click.pass_context
def v3_get_cmd(ctx: click.Context, collection: str, identifier: str) -> None:
    conn = _conn(ctx); _require_url(conn)
    _emit(ctx, v3_mod.v3_get(collection, identifier, conn=conn))


@v3_grp.command("delete")
@click.argument("collection")
@click.argument("identifier")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the interactive confirmation (required for scripted use).")
@click.pass_context
def v3_delete_cmd(ctx: click.Context, collection: str, identifier: str, yes: bool) -> None:
    if not _confirm(ctx, f"Delete {collection}/{identifier}?", yes=yes):
        raise click.ClickException("aborted")
    conn = _conn(ctx); _require_url(conn)
    if _dry_run_block(ctx, f"DELETE /v3/{collection}/{identifier}"):
        return
    res = v3_mod.v3_delete(collection, identifier, conn=conn)
    _maybe_save_session(ctx, action=f"v3.{collection}.delete", detail=identifier)
    _emit(ctx, res, human=f"deleted {collection}/{identifier}")


# ─── treatments: bg-check convenience ──────────────────────────────────────

@treatments_grp.command("bg-check")
@click.option("--glucose", required=True, type=float)
@click.option("--glucose-type", default="Finger",
                type=click.Choice(["Finger", "Sensor", "Manual"]))
@click.option("--notes", default=None)
@click.pass_context
def treatments_bg_check(ctx: click.Context, glucose: float,
                           glucose_type: str, notes: str | None) -> None:
    """Post a BG Check treatment (finger-stick / sensor read)."""
    conn = _conn(ctx); _require_url(conn)
    if _dry_run_block(ctx, "POST /treatments.json (BG Check)",
                       payload={"glucose": glucose,
                                "glucoseType": glucose_type, "notes": notes}):
        return
    res = treatments_mod.add_bg_check(
        glucose=glucose, glucose_type=glucose_type, notes=notes, conn=conn)
    _maybe_save_session(ctx, action="treatments.bg_check",
                          detail=f"{glucose} ({glucose_type})")
    _emit(ctx, res, human=f"recorded BG {glucose} ({glucose_type})")


# ─── watch (real-time, requires optional extra) ────────────────────────────

@cli.group("watch")
def watch_grp() -> None:
    """Real-time entries/treatments via socket.io (requires `pip install '.[watch]'`)."""


@watch_grp.command("entries")
@click.option("--timeout", default=None, type=float,
                help="Stop after this many seconds")
@click.pass_context
def watch_entries_cmd(ctx: click.Context, timeout: float | None) -> None:
    """Stream live SGV updates to stdout."""
    conn = _conn(ctx); _require_url(conn)
    def _cb(entry: dict) -> None:
        click.echo(f"[sgv] {entry.get('dateString','?')[:19]}  sgv={entry.get('sgv','?')}  dir={entry.get('direction','?')}")
    watch_mod.watch_entries(conn=conn, callback=_cb, timeout=timeout)


@watch_grp.command("treatments")
@click.option("--timeout", default=None, type=float)
@click.pass_context
def watch_treatments_cmd(ctx: click.Context, timeout: float | None) -> None:
    """Stream live treatment events to stdout."""
    conn = _conn(ctx); _require_url(conn)
    def _cb(t: dict) -> None:
        click.echo(f"[tx] {t.get('created_at','?')[:19]}  {t.get('eventType','?')}  carbs={t.get('carbs')}  ins={t.get('insulin')}")
    watch_mod.watch_treatments(conn=conn, callback=_cb, timeout=timeout)


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


# ─── new: helpers for v3 body parsing ──────────────────────────────────────

def _load_body(body_json: str | None, body_file: str | None) -> dict[str, Any]:
    """Resolve --body-json (literal JSON) or --body-file (path) into a dict.

    Raises ClickException if the parsed body is anything other than a dict —
    profile / devicestatus / v3 endpoints all expect a JSON object, and a
    list / scalar / null would silently corrupt the target collection.
    """
    if body_json and body_file:
        raise click.ClickException("pass either --body-json or --body-file, not both")
    if body_file:
        try:
            with open(body_file, "r", encoding="utf-8") as f:
                parsed = json.load(f)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"{body_file} is not valid JSON: {exc}")
    elif body_json:
        try:
            parsed = json.loads(body_json)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"--body-json is not valid JSON: {exc}")
    else:
        raise click.ClickException("missing body — use --body-json '<JSON>' or --body-file <path>")
    if not isinstance(parsed, dict):
        raise click.ClickException(
            f"body must be a JSON object (dict), got {type(parsed).__name__}. "
            f"Posting a {type(parsed).__name__} to a Nightscout document endpoint "
            f"would silently corrupt the collection."
        )
    return parsed


# ─── properties ────────────────────────────────────────────────────────────

@cli.group("properties")
def properties_grp() -> None:
    """Derived state from /api/v2/properties (IOB, COB, bgnow, loop, sensor)."""


@properties_grp.command("get")
@click.argument("names", required=False)
@click.pass_context
def properties_get(ctx: click.Context, names: str | None) -> None:
    """Fetch all properties, or a comma-separated subset (e.g. `iob,cob,loop`)."""
    conn = _conn(ctx); _require_url(conn)
    name_list = [n.strip() for n in names.split(",")] if names else None
    res = properties_mod.properties(conn=conn, names=name_list)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        if not res:
            click.echo("  (empty)")
            return
        iob = (res.get("iob") or {}).get("iob")
        cob = (res.get("cob") or {}).get("cob")
        bgnow = (res.get("bgnow") or {}).get("mean")
        delta = (res.get("delta") or {}).get("mean5MinsAgo")
        loop_label = ((res.get("loop") or {}).get("display") or {}).get("label")
        if iob is not None:
            click.echo(f"  IOB:    {iob}U")
        if cob is not None:
            click.echo(f"  COB:    {cob}g")
        if bgnow is not None:
            click.echo(f"  BG now: {bgnow}")
        if delta is not None:
            click.echo(f"  Δ5min:  {delta:+}")
        if loop_label:
            click.echo(f"  Loop:   {loop_label}")
        keys = ", ".join(sorted(res.keys()))
        click.echo(f"  keys:   {keys}")


# ─── v3: create / update / patch / search / history ─────────────────────────

@v3_grp.command("create")
@click.argument("collection")
@click.option("--body-json", default=None, help="Inline JSON string")
@click.option("--body-file", default=None, type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def v3_create_cmd(ctx: click.Context, collection: str,
                    body_json: str | None, body_file: str | None) -> None:
    """POST a new record into a v3 collection."""
    conn = _conn(ctx); _require_url(conn)
    payload = _load_body(body_json, body_file)
    if _dry_run_block(ctx, f"POST /v3/{collection}", payload=payload):
        return
    res = v3_mod.v3_create(collection, payload, conn=conn)
    _maybe_save_session(ctx, action=f"v3.{collection}.create")
    _emit(ctx, res, human=f"created in {collection}")


@v3_grp.command("update")
@click.argument("collection")
@click.argument("identifier")
@click.option("--body-json", default=None)
@click.option("--body-file", default=None, type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def v3_update_cmd(ctx: click.Context, collection: str, identifier: str,
                    body_json: str | None, body_file: str | None) -> None:
    """PUT a full replacement for a v3 record."""
    conn = _conn(ctx); _require_url(conn)
    payload = _load_body(body_json, body_file)
    if _dry_run_block(ctx, f"PUT /v3/{collection}/{identifier}", payload=payload):
        return
    res = v3_mod.v3_update(collection, identifier, payload, conn=conn)
    _maybe_save_session(ctx, action=f"v3.{collection}.update", detail=identifier)
    _emit(ctx, res, human=f"updated {collection}/{identifier}")


@v3_grp.command("patch")
@click.argument("collection")
@click.argument("identifier")
@click.option("--body-json", default=None)
@click.option("--body-file", default=None, type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def v3_patch_cmd(ctx: click.Context, collection: str, identifier: str,
                   body_json: str | None, body_file: str | None) -> None:
    """PATCH a partial update for a v3 record."""
    conn = _conn(ctx); _require_url(conn)
    payload = _load_body(body_json, body_file)
    if _dry_run_block(ctx, f"PATCH /v3/{collection}/{identifier}", payload=payload):
        return
    res = v3_mod.v3_patch(collection, identifier, payload, conn=conn)
    _maybe_save_session(ctx, action=f"v3.{collection}.patch", detail=identifier)
    _emit(ctx, res, human=f"patched {collection}/{identifier}")


@v3_grp.command("search")
@click.argument("collection")
@click.option("--query", "-q", default=None, help="Regex (matched against --field columns)")
@click.option("--field", "-f", multiple=True,
                help="Field(s) to apply --query to (default: notes,eventType)")
@click.option("--filter", "raw_filter", multiple=True,
                help="Explicit v3 filter: key=value e.g. 'created_at$gte=2025-01-01'")
@click.option("--limit", default=100, type=int)
@click.option("--sort", default=None)
@click.pass_context
def v3_search_cmd(ctx: click.Context, collection: str, query: str | None,
                    field: tuple[str, ...], raw_filter: tuple[str, ...],
                    limit: int, sort: str | None) -> None:
    """Search a v3 collection by regex query and/or explicit filters."""
    conn = _conn(ctx); _require_url(conn)
    flt: dict[str, Any] = {}
    for f in raw_filter:
        if "=" not in f:
            raise click.ClickException(f"--filter expects key=value, got {f!r}")
        k, v = f.split("=", 1)
        flt[k.strip()] = v.strip()
    fields = field or ("notes", "eventType")
    res = v3_mod.v3_search(collection, conn=conn, query=query,
                              fields=fields, filter=flt or None,
                              limit=limit, sort=sort)
    _emit(ctx, res, human=f"  {len(res)} match(es) in '{collection}'")


@v3_grp.command("history")
@click.argument("collection")
@click.option("--since-ms", default=None, type=int,
                help="Epoch ms — return changes since this time")
@click.option("--limit", default=100, type=int)
@click.pass_context
def v3_history_cmd(ctx: click.Context, collection: str,
                     since_ms: int | None, limit: int) -> None:
    """Fetch v3 collection mutation history (for incremental sync)."""
    conn = _conn(ctx); _require_url(conn)
    res = v3_mod.v3_history(collection, conn=conn,
                                last_modified_ms=since_ms, limit=limit)
    _emit(ctx, res, human=f"  {len(res)} history entr(ies) in '{collection}'")


# ─── treatments: update ─────────────────────────────────────────────────────

@treatments_grp.command("update")
@click.argument("spec")
@click.option("--carbs", type=float, default=None)
@click.option("--insulin", type=float, default=None)
@click.option("--glucose", type=float, default=None)
@click.option("--notes", default=None)
@click.option("--event-type", default=None)
@click.option("--created-at", default=None)
@click.pass_context
def treatments_update(ctx: click.Context, spec: str, carbs: float | None,
                        insulin: float | None, glucose: float | None,
                        notes: str | None, event_type: str | None,
                        created_at: str | None) -> None:
    """Edit a treatment record by _id (PUT /api/v1/treatments)."""
    conn = _conn(ctx); _require_url(conn)
    fields: dict[str, Any] = {}
    if carbs is not None: fields["carbs"] = carbs
    if insulin is not None: fields["insulin"] = insulin
    if glucose is not None: fields["glucose"] = glucose
    if notes is not None: fields["notes"] = notes
    if event_type is not None: fields["eventType"] = event_type
    if created_at is not None: fields["created_at"] = created_at
    if not fields:
        raise click.ClickException("no fields to update — pass at least one of --carbs/--insulin/--glucose/--notes/--event-type/--created-at")
    if _dry_run_block(ctx, f"PUT /treatments.json (_id={spec})", payload=fields):
        return
    res = treatments_mod.update_treatment(spec, fields, conn=conn)
    _maybe_save_session(ctx, action="treatments.update", detail=spec)
    _emit(ctx, res, human=f"updated treatment {spec}")


# ─── food: quickpicks / regular / add / update / delete ────────────────────

@food_grp.command("quickpicks")
@click.pass_context
def food_quickpicks(ctx: click.Context) -> None:
    """Foods flagged as quickpicks (mobile-client favourites)."""
    conn = _conn(ctx); _require_url(conn)
    res = food_mod.quickpicks(conn=conn)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        click.echo(f"  {len(res)} quickpicks")
        for f in res:
            click.echo(f"  {f.get('food','?'):<30} {f.get('carbs','?')}g / {f.get('portion','?')} {f.get('unit','')}")


@food_grp.command("regular")
@click.pass_context
def food_regular(ctx: click.Context) -> None:
    """Non-quickpick foods."""
    conn = _conn(ctx); _require_url(conn)
    res = food_mod.regular(conn=conn)
    _emit(ctx, res, human=f"  {len(res)} regular food items")


@food_grp.command("add")
@click.option("--food", required=True, help="Food name")
@click.option("--carbs", required=True, type=float)
@click.option("--portion", required=True, type=float)
@click.option("--unit", default="g")
@click.option("--category", default=None)
@click.option("--subcategory", default=None)
@click.option("--gi", type=int, default=None)
@click.option("--energy", type=float, default=None)
@click.option("--quickpick", is_flag=True, default=False)
@click.pass_context
def food_add(ctx: click.Context, food: str, carbs: float, portion: float,
              unit: str, category: str | None, subcategory: str | None,
              gi: int | None, energy: float | None, quickpick: bool) -> None:
    """Add a food entry."""
    conn = _conn(ctx); _require_url(conn)
    if _dry_run_block(ctx, "POST /v3/food",
                       payload={"food": food, "carbs": carbs,
                                "portion": portion, "unit": unit}):
        return
    res = food_mod.add_food(food=food, carbs=carbs, portion=portion, unit=unit,
                              category=category, subcategory=subcategory, gi=gi,
                              energy=energy, quickpick=quickpick, conn=conn)
    _maybe_save_session(ctx, action="food.add", detail=food)
    _emit(ctx, res, human=f"added food: {food}")


@food_grp.command("update")
@click.argument("food_id")
@click.option("--food", default=None)
@click.option("--carbs", type=float, default=None)
@click.option("--portion", type=float, default=None)
@click.option("--unit", default=None)
@click.option("--gi", type=int, default=None)
@click.option("--energy", type=float, default=None)
@click.pass_context
def food_update(ctx: click.Context, food_id: str, food: str | None,
                  carbs: float | None, portion: float | None, unit: str | None,
                  gi: int | None, energy: float | None) -> None:
    """Update a food entry by _id."""
    conn = _conn(ctx); _require_url(conn)
    fields: dict[str, Any] = {}
    if food is not None: fields["food"] = food
    if carbs is not None: fields["carbs"] = carbs
    if portion is not None: fields["portion"] = portion
    if unit is not None: fields["unit"] = unit
    if gi is not None: fields["gi"] = gi
    if energy is not None: fields["energy"] = energy
    if not fields:
        raise click.ClickException("no fields to update")
    if _dry_run_block(ctx, f"PATCH /v3/food/{food_id}", payload=fields):
        return
    res = food_mod.update_food(food_id, fields, conn=conn)
    _maybe_save_session(ctx, action="food.update", detail=food_id)
    _emit(ctx, res, human=f"updated food {food_id}")


@food_grp.command("delete")
@click.argument("food_id")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the interactive confirmation (required for scripted use).")
@click.pass_context
def food_delete(ctx: click.Context, food_id: str, yes: bool) -> None:
    """Delete a food entry by _id."""
    if not _confirm(ctx, f"Delete food {food_id}?", yes=yes):
        raise click.ClickException("aborted")
    conn = _conn(ctx); _require_url(conn)
    if _dry_run_block(ctx, f"DELETE /v3/food/{food_id}"):
        return
    res = food_mod.delete_food(food_id, conn=conn)
    _maybe_save_session(ctx, action="food.delete", detail=food_id)
    _emit(ctx, res, human=f"deleted food {food_id}")


# ─── profile: get-named / setting-at / create / update / delete ────────────

@profile_grp.command("get-named")
@click.argument("name")
@click.pass_context
def profile_get_named(ctx: click.Context, name: str) -> None:
    """Fetch a specific named profile body from the current record."""
    conn = _conn(ctx); _require_url(conn)
    body = profile_mod.current_named(name, conn=conn)
    if body is None:
        _emit(ctx, {}, human=f"no profile named {name!r} in the current record")
        return
    _emit(ctx, body)


@profile_grp.command("setting-at")
@click.option("--field", "field", required=True,
                type=click.Choice(["basal", "carbratio", "sens",
                                    "target_low", "target_high"]))
@click.option("--at", "at_time", default=None, help="HH:MM (default: now)")
@click.option("--name", default=None,
                help="Named profile (default: active/defaultProfile)")
@click.pass_context
def profile_setting_at(ctx: click.Context, field: str, at_time: str | None,
                          name: str | None) -> None:
    """Single-field schedule lookup at HH:MM."""
    from datetime import datetime
    conn = _conn(ctx); _require_url(conn)
    if at_time is None:
        at_time = datetime.now().strftime("%H:%M")
    store = (profile_mod.current_named(name, conn=conn)
                if name else profile_mod.current_store(conn=conn))
    if not store:
        _emit(ctx, {}, human="no active profile found"); return
    value = profile_mod.setting_at(store, field, at_time)
    _emit(ctx, {"field": field, "at": at_time, "value": value,
                "name": name or "(default)"},
            human=f"  {field} at {at_time}: {value if value is not None else '—'}")


@profile_grp.command("create")
@click.option("--body-json", default=None)
@click.option("--body-file", default=None, type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def profile_create(ctx: click.Context, body_json: str | None,
                     body_file: str | None) -> None:
    """Create a new profile record (POST /api/v1/profile)."""
    conn = _conn(ctx); _require_url(conn)
    payload = _load_body(body_json, body_file)
    if _dry_run_block(ctx, "POST /profile.json", payload=payload):
        return
    res = profile_mod.create_profile(payload, conn=conn)
    _maybe_save_session(ctx, action="profile.create")
    _emit(ctx, res, human="created profile record")


@profile_grp.command("update")
@click.argument("profile_id")
@click.option("--body-json", default=None,
                help="JSON dict of field changes to merge onto the record")
@click.option("--body-file", default=None, type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def profile_update(ctx: click.Context, profile_id: str,
                     body_json: str | None, body_file: str | None) -> None:
    """Update a profile record by _id (PUT /api/v1/profile)."""
    conn = _conn(ctx); _require_url(conn)
    fields = _load_body(body_json, body_file)
    if _dry_run_block(ctx, f"PUT /profile.json (_id={profile_id})", payload=fields):
        return
    res = profile_mod.update_profile(profile_id, fields, conn=conn)
    _maybe_save_session(ctx, action="profile.update", detail=profile_id)
    _emit(ctx, res, human=f"updated profile {profile_id}")


@profile_grp.command("delete")
@click.argument("profile_id")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the interactive confirmation (required for scripted use).")
@click.pass_context
def profile_delete(ctx: click.Context, profile_id: str, yes: bool) -> None:
    """Delete a profile record by _id."""
    if not _confirm(ctx, f"Delete profile record {profile_id}?", yes=yes):
        raise click.ClickException("aborted")
    conn = _conn(ctx); _require_url(conn)
    if _dry_run_block(ctx, f"DELETE /profile/{profile_id}.json"):
        return
    res = profile_mod.delete_profile(profile_id, conn=conn)
    _maybe_save_session(ctx, action="profile.delete", detail=profile_id)
    _emit(ctx, res, human=f"deleted profile {profile_id}")


# ─── entries: current / count / times / normalize ───────────────────────────

@entries_grp.command("current")
@click.pass_context
def entries_current(ctx: click.Context) -> None:
    """Latest sgv via /api/v1/entries/current (lighter than `latest`)."""
    conn = _conn(ctx); _require_url(conn)
    res = entries_mod.current(conn=conn)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        rows = res if isinstance(res, list) else [res] if res else []
        click.echo("  date                          sgv   direction")
        for e in rows:
            if isinstance(e, dict):
                click.echo(_format_entry_row(e))


@entries_grp.command("count")
@click.option("--storage", default="entries")
@click.option("--field", default=None,
                help="Field name to filter on (e.g. 'type')")
@click.option("--op", default=None,
                help="Operator without leading $ (e.g. 'eq', 'gte')")
@click.option("--value", default=None, help="Value for the filter expression")
@click.pass_context
def entries_count(ctx: click.Context, storage: str, field: str | None,
                    op: str | None, value: str | None) -> None:
    """Server-side count over a storage collection."""
    conn = _conn(ctx); _require_url(conn)
    res = entries_mod.count_records(storage=storage, field=field, op=op,
                                       value=value, conn=conn)
    _emit(ctx, res, human=f"  count: {res.get('count', res)}")


@entries_grp.command("times")
@click.argument("prefix")
@click.option("--regex", default=None)
@click.pass_context
def entries_times(ctx: click.Context, prefix: str, regex: str | None) -> None:
    """Time-pattern query (e.g. all entries with dateString matching prefix+regex)."""
    conn = _conn(ctx); _require_url(conn)
    res = entries_mod.times_query(prefix=prefix, regex=regex, conn=conn)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        click.echo(f"  {len(res)} entries match {prefix}{('/' + regex) if regex else ''}")
        for e in res[:50]:
            click.echo(_format_entry_row(e))


@entries_grp.command("normalize")
@click.option("--to-units", required=True, type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.option("--from-units", default="mg/dl",
                type=click.Choice(["mg/dl", "mmol", "mmol/l"]))
@click.option("--count", default=50, type=int,
                help="How many recent entries to normalize")
@click.pass_context
def entries_normalize(ctx: click.Context, to_units: str, from_units: str,
                        count: int) -> None:
    """Fetch recent entries and convert sgv/mbg to a target unit (local-only)."""
    conn = _conn(ctx); _require_url(conn)
    data = entries_mod.latest(count=count, conn=conn)
    out = entries_mod.normalize_entries(data, to_units=to_units, from_units=from_units)
    if _is_json(ctx):
        _emit(ctx, out)
    else:
        click.echo(f"  {len(out)} entries → {to_units}")
        for e in out:
            click.echo(_format_entry_row(e))


# ─── status: versions ─────────────────────────────────────────────────────

@status_grp.command("versions")
@click.pass_context
def status_versions(ctx: click.Context) -> None:
    """GET /api/v1/versions — plugin/package manifest."""
    conn = _conn(ctx); _require_url(conn)
    res = status_mod.versions(conn=conn)
    _emit(ctx, res)


# ─── devicestatus: add ────────────────────────────────────────────────────

@devicestatus_grp.command("add")
@click.option("--body-json", default=None, help="Full devicestatus record as JSON")
@click.option("--body-file", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--device", default=None, help="Shortcut: device name")
@click.option("--created-at", default=None,
                help="Shortcut: ISO timestamp (default: now)")
@click.pass_context
def devicestatus_add(ctx: click.Context, body_json: str | None,
                       body_file: str | None, device: str | None,
                       created_at: str | None) -> None:
    """Post a devicestatus snapshot."""
    conn = _conn(ctx); _require_url(conn)
    if body_json or body_file:
        payload = _load_body(body_json, body_file)
    else:
        if not device:
            raise click.ClickException("pass --body-json/--body-file or --device <name>")
        from datetime import datetime, timezone
        payload = {
            "device": device,
            "created_at": created_at or datetime.now(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
    if _dry_run_block(ctx, "POST /devicestatus.json", payload=payload):
        return
    res = ds_mod.add_devicestatus(payload, conn=conn)
    _maybe_save_session(ctx, action="devicestatus.add")
    _emit(ctx, res, human="posted devicestatus")


# ─── notifications ────────────────────────────────────────────────────────

@cli.group("notifications")
def notifications_grp() -> None:
    """Alarms and admin notices."""


@notifications_grp.command("ack")
@click.option("--level", required=True, type=click.IntRange(0, 2),
                help="0=info, 1=warn, 2=urgent")
@click.option("--time-minutes", type=int, default=None,
                help="Silence window in minutes (default: server-side default)")
@click.option("--group", default="default")
@click.pass_context
def notifications_ack(ctx: click.Context, level: int,
                         time_minutes: int | None, group: str) -> None:
    """Acknowledge an outstanding alarm at the given urgency level."""
    conn = _conn(ctx); _require_url(conn)
    if _dry_run_block(ctx, f"POST /notifications/ack (level={level} group={group})",
                       payload={"level": level, "time_minutes": time_minutes,
                                "group": group}):
        return
    res = notifications_mod.ack(level=level, time_minutes=time_minutes,
                                    group=group, conn=conn)
    _maybe_save_session(ctx, action="notifications.ack",
                          detail=f"level={level} group={group}")
    _emit(ctx, res, human=f"acked level={level} group={group}")


@notifications_grp.command("admin")
@click.pass_context
def notifications_admin(ctx: click.Context) -> None:
    """List admin notifications (filtered server-side by permissions)."""
    conn = _conn(ctx); _require_url(conn)
    res = notifications_mod.admin_notifies(conn=conn)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        notifies = res.get("notifies") or []
        click.echo(f"  {res.get('notifyCount', 0)} admin notice(s) "
                    f"({'visible' if notifies else 'hidden — non-admin token'})")
        for n in notifies[:50]:
            click.echo(f"  - {n.get('title','?')}: {n.get('message','?')}")


# ─── report: sensor-life / iob-cob ─────────────────────────────────────────

@report_grp.command("sensor-life")
@click.option("--days", default=30, type=int,
                help="History window for sensor-session detection")
@click.option("--threshold-hours", default=168.0, type=float,
                help="Replace-after threshold in hours (default 168 = 7 days)")
@click.pass_context
def report_sensor_life(ctx: click.Context, days: int,
                          threshold_hours: float) -> None:
    """Current sensor age vs replacement threshold."""
    from datetime import datetime, timedelta, timezone
    conn = _conn(ctx); _require_url(conn)
    end = datetime.now(timezone.utc); start = end - timedelta(days=days)
    txs = treatments_mod.list_treatments(
        conn=conn, count=10000,
        date_gte=start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        date_lte=end.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    sessions = sensors_mod.sensor_sessions(txs)
    res = sensors_mod.sensor_life_report(sessions, threshold_hours=threshold_hours)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        cs = res.get("current_session")
        if not cs:
            click.echo("  no sensor sessions detected in window"); return
        click.echo(f"  current sensor:  started {cs['start'][:19]}  ({cs['marker_event_type']})")
        click.echo(f"  age:             {res['age_hours']}h")
        click.echo(f"  threshold:       {res['threshold_hours']}h")
        click.echo(f"  remaining:       {res['hours_remaining']}h")
        if res["is_stale"]:
            click.echo("  status:          STALE — replace now")
        elif res["should_replace_soon"]:
            click.echo("  status:          replace within 12h")
        else:
            click.echo("  status:          fresh")


@report_grp.command("iob-cob")
@click.pass_context
def report_iob_cob(ctx: click.Context) -> None:
    """One-call IOB/COB/bgnow/delta/loop snapshot from /api/v2/properties."""
    conn = _conn(ctx); _require_url(conn)
    res = properties_mod.iob_cob_report(conn=conn)
    if _is_json(ctx):
        _emit(ctx, res)
    else:
        s = res.get("summary") or {}
        click.echo(f"  IOB:        {s.get('iob') if s.get('iob') is not None else '—'} U")
        click.echo(f"  COB:        {s.get('cob') if s.get('cob') is not None else '—'} g")
        click.echo(f"  BG now:     {s.get('bgnow') if s.get('bgnow') is not None else '—'}")
        click.echo(f"  Δ5min:      {s.get('delta_5min') if s.get('delta_5min') is not None else '—'}")
        click.echo(f"  Loop:       {s.get('loop_label') or '—'}")


if __name__ == "__main__":
    main()
