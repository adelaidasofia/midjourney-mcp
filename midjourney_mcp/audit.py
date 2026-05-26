"""JSONL audit log + daily-USD-spend tracker for midjourney-mcp.

4-field per-call observability schema (execution_time_ms, io, token_usage,
error_class) per the MCP Build Runbook. `token_usage` stays empty because
no LLM is in the path; image-gen cost is reported separately via the
`extra.usd_estimate` + `extra.usd_actual` fields and rolled into
`spend.json` for the daily cap check.

Cross-MCP `_shared/` extraction deferred: this is the 3rd MCP carrying the
4-field pattern (godaddy, cloudflare-dns, midjourney). Run 7 imessage-mcp
precedent kept extraction per-MCP at v0.1.0; extraction triggers when a
4th MCP adopts it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_AUDIT_PATH = Path(os.environ.get(
    "MIDJOURNEY_MCP_AUDIT_LOG",
    str(Path.home() / ".claude" / "midjourney-mcp" / "audit.log.jsonl"),
))
_SPEND_PATH = Path(os.environ.get(
    "MIDJOURNEY_MCP_SPEND_FILE",
    str(Path.home() / ".claude" / "midjourney-mcp" / "spend.json"),
))

ERROR_CLASSES = {
    "none",
    "auth",
    "rate_limit",
    "rate_cap",         # local daily-USD cap exceeded (NOT upstream 429)
    "validation",
    "not_found",
    "timeout",
    "upstream_error",
    "internal_error",
}


def _serializable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serializable(v) for v in obj]
    return str(obj)


def write(
    tool: str,
    execution_time_ms: int,
    io: dict[str, Any],
    error_class: str = "none",
    extra: dict[str, Any] | None = None,
) -> None:
    if error_class not in ERROR_CLASSES:
        error_class = "internal_error"
    record = {
        "ts": int(time.time()),
        "tool": tool,
        "execution_time_ms": int(execution_time_ms),
        "io": _serializable(io),
        "token_usage": {},
        "error_class": error_class,
    }
    if extra:
        record["extra"] = _serializable(extra)
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


@contextmanager
def time_call(tool: str, io_input: dict[str, Any]):
    started = time.perf_counter()
    ctx: dict[str, Any] = {"input": io_input, "output": None, "error_class": "none", "extra": None}
    try:
        yield ctx
    except Exception as exc:
        ctx["error_class"] = ctx.get("error_class") or "internal_error"
        ctx["output"] = {"error": f"{exc.__class__.__name__}: {exc}"}
        raise
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        write(
            tool=tool,
            execution_time_ms=elapsed_ms,
            io={"input": ctx["input"], "output": ctx["output"]},
            error_class=ctx["error_class"],
            extra=ctx.get("extra"),
        )


# ---------------------------------------------------------------------------
# Daily-USD-spend tracker
# ---------------------------------------------------------------------------


def _today_key() -> str:
    """Calendar day at the configured UTC offset, as YYYY-MM-DD.

    Default offset is -5 hours (no DST handling — meant for stable offsets like
    UTC-5 / UTC+0 / UTC+1). Override via `MIDJOURNEY_MCP_TZ_OFFSET_HOURS` env var
    (e.g. `-8` for US Pacific Standard, `1` for Central European).
    """
    try:
        offset_hours = float(os.environ.get("MIDJOURNEY_MCP_TZ_OFFSET_HOURS", "-5"))
    except (TypeError, ValueError):
        offset_hours = -5.0
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=offset_hours)
    return now.strftime("%Y-%m-%d")


def _load_spend() -> dict[str, Any]:
    if not _SPEND_PATH.exists():
        return {}
    try:
        with _SPEND_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_spend(data: dict[str, Any]) -> None:
    try:
        _SPEND_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SPEND_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_SPEND_PATH)
    except Exception:
        pass


def daily_cap_usd() -> float:
    raw = os.environ.get("MIDJOURNEY_MCP_DAILY_USD_CAP", "5.00")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 5.00


def spend_today() -> float:
    data = _load_spend()
    rec = data.get(_today_key()) or {}
    try:
        return float(rec.get("usd_total") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cap_status() -> dict[str, Any]:
    """Snapshot of today's cap state. Used by account_info() + cap check."""
    cap = daily_cap_usd()
    spent = spend_today()
    return {
        "date": _today_key(),
        "usd_cap": round(cap, 4),
        "usd_spent": round(spent, 4),
        "usd_remaining": round(max(0.0, cap - spent), 4),
        "cap_reached": spent >= cap and cap > 0.0,
        "calls_today": _calls_today(),
    }


def _calls_today() -> int:
    data = _load_spend()
    rec = data.get(_today_key()) or {}
    try:
        return int(rec.get("calls") or 0)
    except (TypeError, ValueError):
        return 0


def check_cap_or_block(task_type: str, estimated_usd: float) -> tuple[bool, dict[str, Any]]:
    """Returns (ok, payload). If ok=False, the tool should refuse with rate_cap.

    A cap of 0.0 disables the check entirely (Host-Your-Account / unlimited mode).
    """
    cap = daily_cap_usd()
    if cap <= 0.0:
        return True, {"usd_cap": 0.0, "note": "cap disabled (HYA mode or explicit 0)"}
    spent = spend_today()
    projected = spent + estimated_usd
    if projected > cap:
        return False, {
            "ok": False,
            "error_class": "rate_cap",
            "message": (
                f"midjourney-mcp daily USD cap reached: spent ${spent:.4f} + estimated "
                f"${estimated_usd:.4f} for {task_type} would exceed cap ${cap:.4f}. "
                "Raise via MIDJOURNEY_MCP_DAILY_USD_CAP or wait until the next calendar day "
                "at your configured offset (env MIDJOURNEY_MCP_TZ_OFFSET_HOURS, default -5)."
            ),
            "usd_cap": round(cap, 4),
            "usd_spent": round(spent, 4),
            "usd_estimate_for_call": round(estimated_usd, 4),
        }
    return True, {"usd_cap": round(cap, 4), "usd_spent": round(spent, 4), "usd_remaining": round(cap - spent, 4)}


def record_spend(task_type: str, usd_estimate: float, usd_actual: float | None) -> dict[str, Any]:
    """Append spend for one call. Returns the day's running totals.

    Estimates land immediately when the task is submitted. Actual cost is
    backfilled separately (via record_actual()) when the task hits a terminal
    state in get_task() / wait_for_task() if and only if the API surfaced a
    usage.consume value.
    """
    data = _load_spend()
    day = _today_key()
    rec = data.get(day) or {"usd_total": 0.0, "usd_estimated": 0.0, "usd_actual": 0.0, "calls": 0, "by_task": {}}
    rec["usd_estimated"] = round(float(rec.get("usd_estimated", 0.0)) + float(usd_estimate), 4)
    if usd_actual is not None:
        rec["usd_actual"] = round(float(rec.get("usd_actual", 0.0)) + float(usd_actual), 4)
    # usd_total uses actual when known, otherwise estimate (pre-flight defensive).
    rec["usd_total"] = round(float(rec["usd_estimated"]) + 0.0, 4)  # estimated drives cap
    rec["calls"] = int(rec.get("calls", 0)) + 1
    by_task = rec.get("by_task") or {}
    bt = by_task.get(task_type) or {"calls": 0, "usd": 0.0}
    bt["calls"] = int(bt.get("calls", 0)) + 1
    bt["usd"] = round(float(bt.get("usd", 0.0)) + float(usd_estimate), 4)
    by_task[task_type] = bt
    rec["by_task"] = by_task
    data[day] = rec
    _save_spend(data)
    return rec


def record_actual(task_type: str, task_id: str, usd_actual: float) -> None:
    """Backfill actual USD cost from PiAPI's meta.usage.consume value.

    Idempotent across a single (task_id, task_type) pair: actual cost is added
    only once per task, tracked in `seen_task_ids` list on the day record.
    """
    data = _load_spend()
    day = _today_key()
    rec = data.get(day) or {"usd_total": 0.0, "usd_estimated": 0.0, "usd_actual": 0.0, "calls": 0, "by_task": {}}
    seen = set(rec.get("seen_task_ids") or [])
    if task_id in seen:
        return
    rec["usd_actual"] = round(float(rec.get("usd_actual", 0.0)) + float(usd_actual), 4)
    seen.add(task_id)
    rec["seen_task_ids"] = sorted(seen)
    data[day] = rec
    _save_spend(data)
