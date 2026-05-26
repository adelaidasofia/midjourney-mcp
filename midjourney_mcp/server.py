"""midjourney-mcp — FastMCP server for Midjourney image generation via PiAPI.

Tools (11 total):

  Read (execute directly, no cost):
    healthcheck        verify PiAPI key + return cap status
    get_task           single task by id (status + output urls when complete)
    wait_for_task      block until terminal state (convenience wrapper)
    list_recent_tasks  scan local audit log for recent task ids
    account_info       PiAPI account + today's USD cap snapshot

  Cost-incurring (rate-cap-gated via daily USD cap):
    imagine            4-up grid from a prompt
    variation          regenerate variations (V1-V4 / high_/low_variation)
    upscale            isolate + enlarge one of the 4 grid images
    describe           4 prompts from an input image (image-to-prompt)
    blend              merge 2-5 input images into a new 4-up grid

  Lifecycle:
    cancel_task        cancel a non-terminal task

Safety patterns (creative tool, NOT prod-mutating — different shape than
godaddy-mcp / cloudflare-dns-mcp):
  - No draft+confirm. Image generation is creative iteration; draft+confirm
    breaks the loop. Instead: daily USD cap (env MIDJOURNEY_MCP_DAILY_USD_CAP,
    default $5.00) enforced pre-call via estimate. Tools refuse with
    error_class='rate_cap' when the projected day total exceeds cap.
  - Per-call 4-field audit log per the MCP Build Runbook 2026-05-24 schema.
  - sanitize_error() on every error payload crossing into model context.
  - mycelium-security sanitize_or_raise + assert_public_ip on every image
    URL input (describe, blend) per ⚙️ Meta/rules/url-input-safety.md.
  - admin.env auto-load so PIAPI_API_KEY never lands in .mcp.json env block.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from fastmcp import FastMCP

from . import audit, costs
from .client import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    PiAPIClient,
    PiAPIError,
    sanitize_error,
)

mcp = FastMCP("midjourney-mcp")
_CLIENT: PiAPIClient | None = None


def _client() -> PiAPIClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = PiAPIClient()
    return _CLIENT


def _error_payload(exc: Exception, *, tool: str) -> dict[str, Any]:
    if isinstance(exc, PiAPIError):
        return {
            "ok": False,
            "error_class": exc.error_class,
            "status": exc.status,
            "piapi_code": exc.piapi_code,
            "message": sanitize_error(str(exc)),
        }
    return {
        "ok": False,
        "error_class": "internal_error",
        "message": sanitize_error(f"{exc.__class__.__name__}: {exc}"),
    }


def _normalize_status(raw: Any) -> str:
    if not isinstance(raw, str):
        return "unknown"
    return raw.lower().strip()


def _maybe_record_actual(task_type: str, task: dict[str, Any]) -> None:
    """Backfill actual USD spend from a completed task's meta.usage.consume."""
    try:
        if not isinstance(task, dict):
            return
        if _normalize_status(task.get("status")) != "completed":
            return
        meta = task.get("meta") or {}
        usage = meta.get("usage") if isinstance(meta, dict) else None
        consume = usage.get("consume") if isinstance(usage, dict) else None
        task_id = task.get("task_id") or ""
        usd_actual = costs.usd_from_consume(consume)
        if task_id and usd_actual > 0:
            audit.record_actual(task_type, task_id, usd_actual)
    except Exception:
        pass


def _check_cap(task_type: str, process_mode: str | None) -> tuple[bool, dict[str, Any], float]:
    estimated = costs.estimate_usd(task_type, process_mode)
    ok, payload = audit.check_cap_or_block(task_type, estimated)
    return ok, payload, estimated


# ---------------------------------------------------------------------------
# Read tools (no cost, no cap)
# ---------------------------------------------------------------------------


@mcp.tool()
def healthcheck() -> dict[str, Any]:
    """Verify the PiAPI key is configured and return today's cap snapshot.

    Does NOT make a network call — PiAPI does not have a dedicated /ping
    endpoint. To verify the key is live, follow up with account_info() or a
    real imagine call. healthcheck is the quick offline status read.
    """
    with audit.time_call("healthcheck", {}) as ctx:
        cap = audit.cap_status()
        out: dict[str, Any] = {
            "ok": True,
            "api_key_present": bool(os.environ.get("PIAPI_API_KEY")),
            "api_base": os.environ.get("PIAPI_API_BASE", "https://api.piapi.ai/api/v1"),
            "cap": cap,
        }
        if not out["api_key_present"]:
            out["ok"] = False
            out["error_class"] = "auth"
            out["message"] = (
                "PIAPI_API_KEY not set. Drop it into ~/.claude/midjourney-mcp/admin.env "
                "(chmod 600). Sign up at https://piapi.ai/ if you don't have an account yet."
            )
            ctx["error_class"] = "auth"
        ctx["output"] = out
        return out


@mcp.tool()
def account_info() -> dict[str, Any]:
    """Get today's USD cap snapshot.

    Returns:
        {
          "ok": True,
          "cap": {"date", "usd_cap", "usd_spent", "usd_remaining", "cap_reached", "calls_today"},
          "process_mode_default": "relax|fast|turbo",
          "estimates_per_task_usd": {"imagine", "variation", "upscale", "describe", "blend"}
        }

    PiAPI does not expose a credits-remaining endpoint via REST as of 2026-05-25;
    track balance in the PiAPI dashboard. Local cap is the operator-controlled
    safety surface.
    """
    with audit.time_call("account_info", {}) as ctx:
        mode = os.environ.get("MIDJOURNEY_MCP_PROCESS_MODE", "fast") or "fast"
        out = {
            "ok": True,
            "cap": audit.cap_status(),
            "process_mode_default": mode,
            "estimates_per_task_usd": {
                k: costs.estimate_usd(k, mode) for k in ("imagine", "variation", "upscale", "describe", "blend")
            },
            "note": (
                "Per-call PiAPI 'consume' lands in audit.log.jsonl on task completion. "
                "Check PiAPI dashboard for total credit balance."
            ),
        }
        ctx["output"] = {"date": out["cap"]["date"], "spent": out["cap"]["usd_spent"]}
        return out


@mcp.tool()
def get_task(task_id: str) -> dict[str, Any]:
    """Get the current state of a Midjourney task by ID.

    Returns the full task object including status (Staged | Pending | Processing
    | Completed | Failed), output (image URLs on completion), and meta.usage
    (PiAPI credit consume). When status == Completed, the audit log backfills
    the actual USD cost for the day.
    """
    with audit.time_call("get_task", {"task_id": task_id}) as ctx:
        try:
            task = _client().get_task(task_id)
            status = _normalize_status(task.get("status"))
            # Figure out task_type from the task itself so we attribute the actual cost correctly.
            task_type = task.get("task_type") if isinstance(task, dict) else None
            if isinstance(task_type, str):
                _maybe_record_actual(task_type, task)
            ctx["output"] = {"status": status}
            return {
                "ok": True,
                "task_id": task_id,
                "status": status,
                "task": task,
            }
        except PiAPIError as exc:
            ctx["error_class"] = exc.error_class
            out = _error_payload(exc, tool="get_task")
            ctx["output"] = out
            return out


@mcp.tool()
def wait_for_task(
    task_id: str,
    timeout_seconds: int = 180,
    poll_interval_seconds: float = 3.0,
) -> dict[str, Any]:
    """Poll a task until it reaches a terminal state or the timeout fires.

    Args:
        task_id: PiAPI task identifier (from imagine/variation/upscale/describe/blend).
        timeout_seconds: Total wait budget. PiAPI fast-mode jobs typically complete in
            30-60s; relax mode can take minutes. Default 180s (3 min) covers fast.
        poll_interval_seconds: Seconds between polls. Default 3s respects the typical
            "1 request per few seconds" pattern. PiAPI doesn't publish per-poll rate
            limits, but 3s is conservative for a few-minute job.

    Returns the final task object on completion / failure, or a timeout error.
    """
    with audit.time_call(
        "wait_for_task",
        {"task_id": task_id, "timeout_seconds": timeout_seconds, "poll_interval_seconds": poll_interval_seconds},
    ) as ctx:
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        last_status = "unknown"
        polls = 0
        last_task: dict[str, Any] | None = None
        try:
            while time.monotonic() < deadline:
                task = _client().get_task(task_id)
                last_task = task if isinstance(task, dict) else last_task
                last_status = _normalize_status(task.get("status"))
                polls += 1
                if last_status in TERMINAL_STATUSES:
                    task_type = task.get("task_type") if isinstance(task, dict) else None
                    if isinstance(task_type, str):
                        _maybe_record_actual(task_type, task)
                    ctx["output"] = {"status": last_status, "polls": polls}
                    return {
                        "ok": True,
                        "task_id": task_id,
                        "status": last_status,
                        "polls": polls,
                        "task": task,
                    }
                time.sleep(max(0.5, float(poll_interval_seconds)))
            out = {
                "ok": False,
                "error_class": "timeout",
                "message": (
                    f"wait_for_task timed out after {timeout_seconds}s ({polls} polls). "
                    f"Last status: {last_status}. Re-poll with get_task(task_id={task_id!r}) when ready."
                ),
                "task_id": task_id,
                "status": last_status,
                "polls": polls,
                "task": last_task,
            }
            ctx["error_class"] = "timeout"
            ctx["output"] = {"status": last_status, "polls": polls}
            return out
        except PiAPIError as exc:
            ctx["error_class"] = exc.error_class
            out = _error_payload(exc, tool="wait_for_task")
            out["last_status"] = last_status
            out["polls"] = polls
            ctx["output"] = out
            return out


@mcp.tool()
def list_recent_tasks(limit: int = 20) -> dict[str, Any]:
    """Scan the local audit log for recently-submitted Midjourney tasks.

    PiAPI does not expose a list-tasks endpoint, so this reads from
    audit.log.jsonl — every submission emits a row containing the returned
    task_id in the extra field. Useful for recovering a task_id you didn't
    save from the imagine response.
    """
    with audit.time_call("list_recent_tasks", {"limit": limit}) as ctx:
        rows: list[dict[str, Any]] = []
        try:
            log_path = audit._AUDIT_PATH  # type: ignore[attr-defined]
            if log_path.exists():
                import json
                with log_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        extra = r.get("extra") or {}
                        task_id = extra.get("task_id") if isinstance(extra, dict) else None
                        if not task_id:
                            # also surface tasks from io.output
                            io = r.get("io") or {}
                            out_payload = io.get("output") if isinstance(io, dict) else None
                            if isinstance(out_payload, dict):
                                task_id = out_payload.get("task_id")
                        if not task_id:
                            continue
                        rows.append({
                            "ts": r.get("ts"),
                            "tool": r.get("tool"),
                            "task_id": task_id,
                            "error_class": r.get("error_class"),
                        })
            rows = rows[-int(max(1, limit)):]
            rows.reverse()
            ctx["output"] = {"count": len(rows)}
            return {"ok": True, "tasks": rows, "count": len(rows)}
        except Exception as exc:
            ctx["error_class"] = "internal_error"
            return _error_payload(exc, tool="list_recent_tasks")


# ---------------------------------------------------------------------------
# Cost-incurring tools (cap-gated)
# ---------------------------------------------------------------------------


def _submit_result_payload(
    task_type: str,
    submit_out: dict[str, Any],
    estimated_usd: float,
    cap_payload: dict[str, Any],
) -> dict[str, Any]:
    """Shape the submission response + record estimated spend."""
    task_id = submit_out.get("task_id") if isinstance(submit_out, dict) else None
    status = _normalize_status(submit_out.get("status") if isinstance(submit_out, dict) else None)
    spend_record = audit.record_spend(task_type, estimated_usd, None)
    return {
        "ok": True,
        "task_id": task_id,
        "status": status,
        "task": submit_out,
        "usd_estimate": round(estimated_usd, 4),
        "usd_spent_today": round(float(spend_record.get("usd_total", 0.0)), 4),
        "usd_cap": cap_payload.get("usd_cap"),
        "next_step": f"call wait_for_task(task_id={task_id!r}) — or get_task(...) to poll once",
    }


@mcp.tool()
def imagine(
    prompt: str,
    aspect_ratio: str = "1:1",
    process_mode: str | None = None,
    skip_prompt_check: bool = False,
) -> dict[str, Any]:
    """Submit a Midjourney /imagine task.

    Generates a 2x2 grid of 4 images from a text prompt. The task is async —
    submit returns a task_id; poll via wait_for_task(task_id) or get_task(task_id)
    until status == Completed, then read image URLs from task.output.

    Args:
        prompt: Text description of the image to generate. Supports Midjourney
            prompt syntax (--ar, --v, --style, etc.) but use aspect_ratio for
            ratio for clarity.
        aspect_ratio: e.g. "1:1", "16:9", "9:16", "3:2", "2:3". Default "1:1".
        process_mode: "relax" | "fast" | "turbo". Defaults to env
            MIDJOURNEY_MCP_PROCESS_MODE, then "fast".
        skip_prompt_check: Skip PiAPI's internal prompt validation. False by default;
            set True only for known-good prompts that PiAPI's filter incorrectly
            rejects.

    Cost: estimated $0.04 USD at fast mode (PAYG). Actual cost lands on completion.
    """
    with audit.time_call(
        "imagine",
        {"prompt_chars": len(prompt or ""), "aspect_ratio": aspect_ratio, "process_mode": process_mode},
    ) as ctx:
        if not isinstance(prompt, str) or not prompt.strip():
            ctx["error_class"] = "validation"
            out = {"ok": False, "error_class": "validation", "message": "prompt must be a non-empty string"}
            ctx["output"] = out
            return out
        ok, cap_payload, estimated = _check_cap("imagine", process_mode)
        if not ok:
            ctx["error_class"] = "rate_cap"
            ctx["output"] = cap_payload
            return cap_payload
        try:
            submit_out = _client().submit_imagine(
                prompt=prompt, aspect_ratio=aspect_ratio,
                process_mode=process_mode, skip_prompt_check=skip_prompt_check,
            )
            result = _submit_result_payload("imagine", submit_out, estimated, cap_payload)
            ctx["output"] = {"task_id": result.get("task_id"), "status": result.get("status")}
            ctx["extra"] = {"task_id": result.get("task_id"), "usd_estimate": estimated}
            return result
        except PiAPIError as exc:
            ctx["error_class"] = exc.error_class
            out = _error_payload(exc, tool="imagine")
            ctx["output"] = out
            return out


@mcp.tool()
def variation(
    origin_task_id: str,
    index: str,
    prompt: str,
    aspect_ratio: str = "1:1",
    process_mode: str | None = None,
    skip_prompt_check: bool = False,
) -> dict[str, Any]:
    """Submit a Midjourney /variation task off an existing imagine.

    Args:
        origin_task_id: task_id from a completed imagine (4-up grid).
        index: "1" | "2" | "3" | "4" (grid position), or "high_variation" /
            "low_variation" (V5.2+/V6 only, off an upscaled image).
        prompt: Variation prompt (often the same as the original).
        aspect_ratio: Inherited from origin if omitted by some APIs; pass to be explicit.
        process_mode: relax | fast | turbo.

    Cost: ~$0.04 USD at fast mode (regenerates a new 4-up grid).
    """
    with audit.time_call(
        "variation",
        {"origin_task_id": origin_task_id, "index": index, "process_mode": process_mode},
    ) as ctx:
        if index not in {"1", "2", "3", "4", "high_variation", "low_variation"}:
            ctx["error_class"] = "validation"
            out = {
                "ok": False, "error_class": "validation",
                "message": "index must be one of '1','2','3','4','high_variation','low_variation'",
            }
            ctx["output"] = out
            return out
        ok, cap_payload, estimated = _check_cap("variation", process_mode)
        if not ok:
            ctx["error_class"] = "rate_cap"
            ctx["output"] = cap_payload
            return cap_payload
        try:
            submit_out = _client().submit_variation(
                origin_task_id=origin_task_id, index=index, prompt=prompt,
                aspect_ratio=aspect_ratio, process_mode=process_mode,
                skip_prompt_check=skip_prompt_check,
            )
            result = _submit_result_payload("variation", submit_out, estimated, cap_payload)
            ctx["output"] = {"task_id": result.get("task_id"), "status": result.get("status")}
            ctx["extra"] = {"task_id": result.get("task_id"), "usd_estimate": estimated}
            return result
        except PiAPIError as exc:
            ctx["error_class"] = exc.error_class
            out = _error_payload(exc, tool="variation")
            ctx["output"] = out
            return out


@mcp.tool()
def upscale(
    origin_task_id: str,
    index: str,
    process_mode: str | None = None,
) -> dict[str, Any]:
    """Submit a Midjourney /upscale task off an existing imagine.

    Args:
        origin_task_id: task_id from a completed imagine.
        index: One of:
            "1" | "2" | "3" | "4"          (grid position; isolates that image)
            "light" | "beta"               (V4-era variant modes)
            "2x" | "4x"                    (V5 variants)
            "subtle" | "creative"          (V6 variants)
        process_mode: relax | fast | turbo.

    Cost: ~$0.01 USD at fast mode (single image, cheaper than imagine).
    """
    with audit.time_call(
        "upscale",
        {"origin_task_id": origin_task_id, "index": index, "process_mode": process_mode},
    ) as ctx:
        valid = {"1", "2", "3", "4", "light", "beta", "2x", "4x", "subtle", "creative"}
        if index not in valid:
            ctx["error_class"] = "validation"
            out = {
                "ok": False, "error_class": "validation",
                "message": f"index must be one of {sorted(valid)}",
            }
            ctx["output"] = out
            return out
        ok, cap_payload, estimated = _check_cap("upscale", process_mode)
        if not ok:
            ctx["error_class"] = "rate_cap"
            ctx["output"] = cap_payload
            return cap_payload
        try:
            submit_out = _client().submit_upscale(
                origin_task_id=origin_task_id, index=index, process_mode=process_mode,
            )
            result = _submit_result_payload("upscale", submit_out, estimated, cap_payload)
            ctx["output"] = {"task_id": result.get("task_id"), "status": result.get("status")}
            ctx["extra"] = {"task_id": result.get("task_id"), "usd_estimate": estimated}
            return result
        except PiAPIError as exc:
            ctx["error_class"] = exc.error_class
            out = _error_payload(exc, tool="upscale")
            ctx["output"] = out
            return out


@mcp.tool()
def describe(image_url: str, process_mode: str | None = None) -> dict[str, Any]:
    """Submit a Midjourney /describe task — image-to-prompt.

    PiAPI fetches the image at image_url and returns 4 candidate prompts that
    Midjourney would generate something similar from. Useful for reverse-
    engineering style + composition from a reference image.

    Args:
        image_url: HTTPS URL ending in a valid image extension. SSRF-guarded
            via mycelium-security (refuses private / link-local / metadata-service
            hosts before forwarding).
        process_mode: relax | fast | turbo. Describe is text-out only; fast is
            usually fine.

    Cost: ~$0.005 USD at fast mode (text out, no image gen). Cheap.
    """
    with audit.time_call(
        "describe",
        {"image_url_host": _safe_host(image_url), "process_mode": process_mode},
    ) as ctx:
        ok, cap_payload, estimated = _check_cap("describe", process_mode)
        if not ok:
            ctx["error_class"] = "rate_cap"
            ctx["output"] = cap_payload
            return cap_payload
        try:
            submit_out = _client().submit_describe(image_url=image_url, process_mode=process_mode)
            result = _submit_result_payload("describe", submit_out, estimated, cap_payload)
            ctx["output"] = {"task_id": result.get("task_id"), "status": result.get("status")}
            ctx["extra"] = {"task_id": result.get("task_id"), "usd_estimate": estimated}
            return result
        except PiAPIError as exc:
            ctx["error_class"] = exc.error_class
            out = _error_payload(exc, tool="describe")
            ctx["output"] = out
            return out


@mcp.tool()
def blend(
    image_urls: list[str],
    dimension: str | None = None,
    process_mode: str | None = None,
) -> dict[str, Any]:
    """Submit a Midjourney /blend task — merge 2-5 images into a new 4-up grid.

    Args:
        image_urls: List of 2 to 5 HTTPS image URLs. Each URL is SSRF-guarded
            via mycelium-security before forwarding to PiAPI.
        dimension: "square" | "portrait" | "landscape". Optional; defaults to square.
        process_mode: relax | fast | turbo.

    Cost: ~$0.04 USD at fast mode (regenerates a 4-up grid).
    """
    with audit.time_call(
        "blend",
        {"image_count": len(image_urls) if isinstance(image_urls, list) else 0,
         "dimension": dimension, "process_mode": process_mode},
    ) as ctx:
        ok, cap_payload, estimated = _check_cap("blend", process_mode)
        if not ok:
            ctx["error_class"] = "rate_cap"
            ctx["output"] = cap_payload
            return cap_payload
        try:
            submit_out = _client().submit_blend(
                image_urls=image_urls, dimension=dimension, process_mode=process_mode,
            )
            result = _submit_result_payload("blend", submit_out, estimated, cap_payload)
            ctx["output"] = {"task_id": result.get("task_id"), "status": result.get("status")}
            ctx["extra"] = {"task_id": result.get("task_id"), "usd_estimate": estimated}
            return result
        except PiAPIError as exc:
            ctx["error_class"] = exc.error_class
            out = _error_payload(exc, tool="blend")
            ctx["output"] = out
            return out


@mcp.tool()
def cancel_task(task_id: str) -> dict[str, Any]:
    """Cancel a non-terminal Midjourney task.

    Idempotent on the model side: PiAPI returns success even when the task has
    already reached a terminal state. No refund mechanism is exposed via REST.
    """
    with audit.time_call("cancel_task", {"task_id": task_id}) as ctx:
        try:
            result = _client().cancel_task(task_id)
            ctx["output"] = {"task_id": task_id}
            return {"ok": True, "task_id": task_id, "result": result}
        except PiAPIError as exc:
            ctx["error_class"] = exc.error_class
            out = _error_payload(exc, tool="cancel_task")
            ctx["output"] = out
            return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_host(url: Any) -> str:
    """Extract host from URL for audit logging without exposing query strings."""
    if not isinstance(url, str):
        return ""
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or ""
    except Exception:
        return ""


if __name__ == "__main__":
    mcp.run()
