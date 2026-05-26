"""midjourney-mcp smoke tests — no network calls, no PiAPI credentials required.

Mirrors the shape of cloudflare-dns-mcp / godaddy-mcp tests:
  - Audit-log redaction override.
  - sanitize_error strips X-API-Key / Bearer / api_key= / Authorization patterns.
  - PiAPI error classification table.
  - Daily-USD cap blocks cost-incurring tools above the cap.
  - Cap == 0.0 disables the check entirely (HYA mode).
  - validate_image_url refuses obvious SSRF targets when mycelium-security is installed.
  - server imports cleanly without credentials (lazy client singleton).
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

_MCP_DIR = Path(__file__).resolve().parent.parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))


def _reload(monkeypatch, **env):
    """Reload audit + costs + client + server with env overrides applied."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, str(v))
    for mod_name in ("audit", "costs", "client", "server"):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
    return importlib.import_module("server")


def test_audit_redaction_uses_override(tmp_path, monkeypatch):
    log = tmp_path / "audit.log.jsonl"
    monkeypatch.setenv("MIDJOURNEY_MCP_AUDIT_LOG", str(log))
    import audit  # type: ignore
    importlib.reload(audit)
    audit.write("smoke", 5, {"input": {"x": 1}, "output": {"y": "z"}}, "none", extra={"task_id": "tk_abc"})
    assert log.exists()
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert any(r["tool"] == "smoke" and r["extra"]["task_id"] == "tk_abc" for r in rows)


def test_sanitize_error_strips_secret_patterns():
    from client import sanitize_error  # type: ignore
    raw = "X-API-Key: pk_live_abc was rejected; Authorization: Bearer mysecret; api_key=oops; token=lol"
    cleaned = sanitize_error(raw)
    assert "pk_live_abc" not in cleaned
    assert "mysecret" not in cleaned
    assert "oops" not in cleaned
    assert "lol" not in cleaned


def test_piapi_error_classification():
    from client import _classify  # type: ignore
    assert _classify(401, None, "") == "auth"
    assert _classify(403, None, "") == "auth"
    assert _classify(404, None, "") == "not_found"
    assert _classify(429, None, "") == "rate_limit"
    assert _classify(408, None, "") == "timeout"
    assert _classify(500, None, "") == "upstream_error"
    assert _classify(400, 422, "") == "validation"
    assert _classify(200, 401, "") == "auth"
    assert _classify(200, 429, "") == "rate_limit"


def test_cost_estimates_scale_with_process_mode(monkeypatch):
    monkeypatch.delenv("MIDJOURNEY_MCP_PROCESS_MODE", raising=False)
    import costs  # type: ignore
    importlib.reload(costs)
    assert costs.estimate_usd("imagine", "relax") < costs.estimate_usd("imagine", "fast")
    assert costs.estimate_usd("imagine", "fast") < costs.estimate_usd("imagine", "turbo")
    assert costs.estimate_usd("upscale", "fast") < costs.estimate_usd("imagine", "fast")


def test_daily_usd_cap_blocks_above_threshold(tmp_path, monkeypatch):
    log = tmp_path / "audit.log.jsonl"
    spend = tmp_path / "spend.json"
    monkeypatch.setenv("MIDJOURNEY_MCP_AUDIT_LOG", str(log))
    monkeypatch.setenv("MIDJOURNEY_MCP_SPEND_FILE", str(spend))
    monkeypatch.setenv("MIDJOURNEY_MCP_DAILY_USD_CAP", "0.05")  # very low cap
    import audit  # type: ignore
    importlib.reload(audit)
    # First imagine fits.
    audit.record_spend("imagine", 0.04, None)
    ok, payload = audit.check_cap_or_block("imagine", 0.04)
    assert not ok
    assert payload["error_class"] == "rate_cap"
    assert payload["usd_cap"] == 0.05


def test_cap_zero_disables_check(tmp_path, monkeypatch):
    spend = tmp_path / "spend.json"
    monkeypatch.setenv("MIDJOURNEY_MCP_SPEND_FILE", str(spend))
    monkeypatch.setenv("MIDJOURNEY_MCP_DAILY_USD_CAP", "0")
    import audit  # type: ignore
    importlib.reload(audit)
    ok, payload = audit.check_cap_or_block("imagine", 9999.0)
    assert ok is True
    assert payload["usd_cap"] == 0.0


def test_validate_image_url_refuses_private_targets(monkeypatch):
    # Skip when the security helper isn't installed in this env (fallback to no-op).
    try:
        import mycelium_security  # noqa: F401
    except ImportError:
        pytest.skip("mycelium-security not installed in this env")
    from client import PiAPIError, validate_image_url  # type: ignore
    for bad in [
        "http://169.254.169.254/latest/meta-data/",   # AWS instance metadata
        "http://127.0.0.1/foo.jpg",
        "http://10.0.0.1/secret.jpg",
        "http://[::1]/x.jpg",
    ]:
        with pytest.raises(PiAPIError) as exc_info:
            validate_image_url(bad)
        assert exc_info.value.error_class == "validation"


def test_server_imports_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("PIAPI_API_KEY", raising=False)
    monkeypatch.setenv("MIDJOURNEY_MCP_AUDIT_LOG", str(tmp_path / "audit.log.jsonl"))
    monkeypatch.setenv("MIDJOURNEY_MCP_SPEND_FILE", str(tmp_path / "spend.json"))
    s = _reload(monkeypatch)
    assert s.mcp is not None
    # healthcheck should not raise; it reports missing key cleanly.
    fn = s.healthcheck.fn if hasattr(s.healthcheck, "fn") else s.healthcheck
    out = fn()
    assert out["ok"] is False
    assert out["error_class"] == "auth"


def test_account_info_returns_cap_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("MIDJOURNEY_MCP_AUDIT_LOG", str(tmp_path / "audit.log.jsonl"))
    monkeypatch.setenv("MIDJOURNEY_MCP_SPEND_FILE", str(tmp_path / "spend.json"))
    monkeypatch.setenv("MIDJOURNEY_MCP_DAILY_USD_CAP", "1.50")
    s = _reload(monkeypatch)
    fn = s.account_info.fn if hasattr(s.account_info, "fn") else s.account_info
    out = fn()
    assert out["ok"] is True
    assert out["cap"]["usd_cap"] == 1.50
    assert out["cap"]["usd_spent"] == 0.0
    assert "imagine" in out["estimates_per_task_usd"]


def test_imagine_refuses_when_cap_reached(monkeypatch, tmp_path):
    monkeypatch.setenv("PIAPI_API_KEY", "fake-key")
    monkeypatch.setenv("MIDJOURNEY_MCP_AUDIT_LOG", str(tmp_path / "audit.log.jsonl"))
    monkeypatch.setenv("MIDJOURNEY_MCP_SPEND_FILE", str(tmp_path / "spend.json"))
    monkeypatch.setenv("MIDJOURNEY_MCP_DAILY_USD_CAP", "0.05")
    s = _reload(monkeypatch)
    # Pre-spend up to cap.
    import audit  # type: ignore
    audit.record_spend("imagine", 0.04, None)
    fn = s.imagine.fn if hasattr(s.imagine, "fn") else s.imagine
    out = fn(prompt="a cat")
    assert out["ok"] is False
    assert out["error_class"] == "rate_cap"


def test_variation_validates_index(monkeypatch, tmp_path):
    monkeypatch.setenv("PIAPI_API_KEY", "fake-key")
    monkeypatch.setenv("MIDJOURNEY_MCP_AUDIT_LOG", str(tmp_path / "audit.log.jsonl"))
    monkeypatch.setenv("MIDJOURNEY_MCP_SPEND_FILE", str(tmp_path / "spend.json"))
    monkeypatch.setenv("MIDJOURNEY_MCP_DAILY_USD_CAP", "10.00")
    s = _reload(monkeypatch)
    fn = s.variation.fn if hasattr(s.variation, "fn") else s.variation
    out = fn(origin_task_id="tk_x", index="5", prompt="a cat")
    assert out["ok"] is False
    assert out["error_class"] == "validation"


def test_upscale_validates_index(monkeypatch, tmp_path):
    monkeypatch.setenv("PIAPI_API_KEY", "fake-key")
    monkeypatch.setenv("MIDJOURNEY_MCP_AUDIT_LOG", str(tmp_path / "audit.log.jsonl"))
    monkeypatch.setenv("MIDJOURNEY_MCP_SPEND_FILE", str(tmp_path / "spend.json"))
    monkeypatch.setenv("MIDJOURNEY_MCP_DAILY_USD_CAP", "10.00")
    s = _reload(monkeypatch)
    fn = s.upscale.fn if hasattr(s.upscale, "fn") else s.upscale
    out = fn(origin_task_id="tk_x", index="bogus")
    assert out["ok"] is False
    assert out["error_class"] == "validation"
