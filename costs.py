"""Estimated USD cost per Midjourney task type for pre-flight cap check.

PiAPI bills per task on PAYG. Actual cost lands in the API response's
`meta.usage.consume` field on completion (in PiAPI credits — 1 credit ~= $0.001
at the time of writing). These pre-call estimates are deliberately conservative
upper bounds for `fast` process_mode so the daily cap kicks in BEFORE the user
overruns rather than after. Update when PiAPI pricing changes.

Pricing references (verified 2026-05-25):
  - PAYG starts at $0.01 per imagine task on PiAPI pricing page.
  - HYA (Host-Your-Account) flat $8/seat/mo — no per-task cost in that mode.

When PIAPI_MODE=hya (Host-Your-Account), set caps to 0.0 so cost tracking
does not block tools.
"""

from __future__ import annotations

import os

# Estimated USD per task at PAYG `fast` mode. `turbo` is roughly 2x; `relax`
# is roughly half. These are pre-flight estimates, not invoiced amounts.
ESTIMATED_USD = {
    "imagine":   0.040,   # 4-up grid
    "variation": 0.040,   # 4-up grid regenerate
    "upscale":   0.010,   # single image
    "describe":  0.005,   # text out, no image gen
    "blend":     0.040,   # 4-up grid from 2-5 inputs
}

# `turbo` is ~2x fast; `relax` is ~0.5x fast.
MODE_MULTIPLIER = {
    "relax": 0.5,
    "fast":  1.0,
    "turbo": 2.0,
}


def estimate_usd(task_type: str, process_mode: str | None = None) -> float:
    """Return the conservative pre-flight USD estimate for one call."""
    base = ESTIMATED_USD.get(task_type, 0.05)
    mode = (process_mode or os.environ.get("MIDJOURNEY_MCP_PROCESS_MODE", "fast") or "fast").lower()
    mult = MODE_MULTIPLIER.get(mode, 1.0)
    return round(base * mult, 4)


def usd_from_consume(consume: float | int | None) -> float:
    """Convert PiAPI's `meta.usage.consume` value (credits) to USD.

    PiAPI's credit conversion: 1 credit ~= $0.001. Source: PiAPI pricing page
    (2026-05). May drift; re-verify when revisiting cost reporting.
    """
    if consume is None:
        return 0.0
    try:
        return round(float(consume) * 0.001, 4)
    except (TypeError, ValueError):
        return 0.0
