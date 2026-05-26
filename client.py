"""PiAPI Midjourney client (synchronous, httpx-backed).

Base URL: https://api.piapi.ai/api/v1
Auth: X-API-Key header (NOT Bearer; PiAPI's convention).

Single submit endpoint (POST /task) with model + task_type + input + config
discriminator shape. Polling via GET /task/{task_id}.

Status values returned by PiAPI: Staged | Pending | Processing | Completed |
Failed. The client normalizes to lowercase strings for switching.

URL inputs (describe, blend) pass through mycelium-security sanitize_or_raise
+ assert_public_ip per ⚙️ Meta/rules/url-input-safety.md before crossing into
the upstream API surface. PiAPI fetches the URL — defense-in-depth still
applies, since the model could be coerced into pointing PiAPI at internal
services.

No personal data ever logged. sanitize_error() strips X-API-Key, api_key=,
Bearer, secret/token/password patterns from any error string crossing into
model context.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    from mycelium_security import UnsafeURL, assert_public_ip, sanitize_or_raise
    _MYCELIUM_SECURITY_AVAILABLE = True
except ImportError:
    _MYCELIUM_SECURITY_AVAILABLE = False

    class UnsafeURL(Exception):
        pass

    def sanitize_or_raise(url: str) -> str:
        return url

    def assert_public_ip(host: str) -> None:
        return None

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_BASE = "https://api.piapi.ai/api/v1"
_ADMIN_ENV_PATH = Path(__file__).resolve().parent / "admin.env"

TERMINAL_STATUSES = {"completed", "failed"}
NON_TERMINAL_STATUSES = {"staged", "pending", "processing"}


def _load_admin_env_if_present() -> None:
    """Read ~/.claude/midjourney-mcp/admin.env (chmod 600, gitignored)."""
    if not _ADMIN_ENV_PATH.exists():
        return
    try:
        for raw_line in _ADMIN_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


class PiAPIError(Exception):
    """Wraps a PiAPI failure with a stable error_class + the PiAPI code."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_class: str = "upstream_error",
        piapi_code: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.error_class = error_class
        self.piapi_code = piapi_code


_SECRET_PATTERNS = [
    re.compile(r"(?i)X-API-Key:\s*[^\s'\"]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|secret|token|password|pwd)=([^&\s'\"]+)"),
    re.compile(r"(?i)Authorization:\s*[^\s'\"]+"),
]


def sanitize_error(text: str) -> str:
    """Strip credentials before crossing into model context."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: f"{m.group(1)}=***" if m.lastindex == 2 else "***REDACTED***", out)
    return out


def _classify(status: int, code: int | None, message: str) -> str:
    """Map PiAPI HTTP status + code to stable error_class."""
    if status == 401 or status == 403:
        return "auth"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limit"
    if status == 408 or "timeout" in (message or "").lower():
        return "timeout"
    if 500 <= status < 600:
        return "upstream_error"
    if 400 <= status < 500:
        # PiAPI returns code != 200 in body for some errors with HTTP 200
        if code in (400, 422):
            return "validation"
        return "validation"
    if isinstance(code, int) and code >= 400:
        if code in (401, 403):
            return "auth"
        if code == 404:
            return "not_found"
        if code == 429:
            return "rate_limit"
        return "upstream_error"
    return "upstream_error"


def validate_image_url(url: str) -> str:
    """Sanitize + SSRF-guard a user-supplied image URL.

    Returns the sanitized URL on success; raises PiAPIError(error_class='validation')
    if the URL is unsafe (private IP, link-local, malformed, etc.).
    """
    if not isinstance(url, str) or not url.strip():
        raise PiAPIError("image URL must be a non-empty string", error_class="validation")
    try:
        safe = sanitize_or_raise(url)
        parsed = urlparse(safe)
        host = parsed.hostname or ""
        assert_public_ip(host)
        return safe
    except UnsafeURL as exc:
        raise PiAPIError(f"refused image URL (SSRF guard): {exc}", error_class="validation") from exc


class PiAPIClient:
    """Thin synchronous HTTP wrapper around the PiAPI task surface."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        _load_admin_env_if_present()
        self.api_key = api_key or os.environ.get("PIAPI_API_KEY", "")
        self.base_url = base_url or os.environ.get("PIAPI_API_BASE", DEFAULT_BASE)
        self.timeout = timeout
        if not self.api_key:
            raise PiAPIError(
                "PIAPI_API_KEY must be set. Drop it into ~/.claude/midjourney-mcp/admin.env "
                "(chmod 600) per the CLAUDE.md credential-storage rule. Sign up at https://piapi.ai/.",
                error_class="auth",
            )

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, *, json_body: Any = None) -> dict[str, Any]:
        url = self._url(path)
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as c:
                r = c.request(method, url, headers=self._headers(), json=json_body)
        except httpx.TimeoutException as exc:
            raise PiAPIError(f"PiAPI timeout after {self.timeout}s", error_class="timeout") from exc
        except httpx.HTTPError as exc:
            raise PiAPIError(
                sanitize_error(f"PiAPI transport error: {exc}"),
                error_class="upstream_error",
            ) from exc

        body: Any
        try:
            body = r.json() if r.content else {}
        except ValueError:
            raise PiAPIError(
                sanitize_error(f"PiAPI {method} {path} -> non-JSON HTTP {r.status_code}: {r.text[:200]}"),
                status=r.status_code,
                error_class=_classify(r.status_code, None, r.text),
            )

        if not isinstance(body, dict):
            raise PiAPIError(
                sanitize_error(f"PiAPI {method} {path} -> non-dict body: {str(body)[:200]}"),
                status=r.status_code,
                error_class="upstream_error",
            )

        code = body.get("code")
        message = body.get("message") or ""
        # PiAPI convention: HTTP 200 with code=200 is success; otherwise read body.code.
        if 200 <= r.status_code < 300 and (code is None or code == 200):
            return body.get("data") if isinstance(body.get("data"), dict) else body

        raise PiAPIError(
            sanitize_error(f"PiAPI {method} {path} -> HTTP {r.status_code} code={code} message={message}"),
            status=r.status_code,
            error_class=_classify(r.status_code, code if isinstance(code, int) else None, message),
            piapi_code=code if isinstance(code, int) else None,
        )

    # ---- Task submit + polling -------------------------------------------

    def _submit(self, task_type: str, input_body: dict[str, Any], process_mode: str | None) -> dict[str, Any]:
        if process_mode and "process_mode" not in input_body:
            input_body = {**input_body, "process_mode": process_mode}
        payload = {
            "model": "midjourney",
            "task_type": task_type,
            "input": input_body,
        }
        return self._request("POST", "/task", json_body=payload)

    def submit_imagine(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        process_mode: str | None = None,
        skip_prompt_check: bool = False,
    ) -> dict[str, Any]:
        input_body = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "skip_prompt_check": bool(skip_prompt_check),
        }
        return self._submit("imagine", input_body, process_mode)

    def submit_variation(
        self,
        origin_task_id: str,
        index: str,
        prompt: str,
        aspect_ratio: str = "1:1",
        process_mode: str | None = None,
        skip_prompt_check: bool = False,
    ) -> dict[str, Any]:
        input_body = {
            "origin_task_id": origin_task_id,
            "index": str(index),
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "skip_prompt_check": bool(skip_prompt_check),
        }
        return self._submit("variation", input_body, process_mode)

    def submit_upscale(
        self,
        origin_task_id: str,
        index: str,
        process_mode: str | None = None,
    ) -> dict[str, Any]:
        input_body = {
            "origin_task_id": origin_task_id,
            "index": str(index),
        }
        return self._submit("upscale", input_body, process_mode)

    def submit_describe(
        self,
        image_url: str,
        process_mode: str | None = None,
    ) -> dict[str, Any]:
        safe_url = validate_image_url(image_url)
        return self._submit("describe", {"image_url": safe_url}, process_mode)

    def submit_blend(
        self,
        image_urls: list[str],
        dimension: str | None = None,
        process_mode: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(image_urls, list) or not (2 <= len(image_urls) <= 5):
            raise PiAPIError("blend requires 2 to 5 image URLs", error_class="validation")
        safe_urls = [validate_image_url(u) for u in image_urls]
        input_body: dict[str, Any] = {"image_urls": safe_urls}
        if dimension:
            if dimension not in ("square", "portrait", "landscape"):
                raise PiAPIError(
                    f"blend dimension must be one of square|portrait|landscape, got {dimension!r}",
                    error_class="validation",
                )
            input_body["dimension"] = dimension
        return self._submit("blend", input_body, process_mode)

    def get_task(self, task_id: str) -> dict[str, Any]:
        if not task_id or not isinstance(task_id, str):
            raise PiAPIError("task_id must be a non-empty string", error_class="validation")
        return self._request("GET", f"/task/{task_id}")

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel a task that hasn't reached a terminal state yet."""
        if not task_id or not isinstance(task_id, str):
            raise PiAPIError("task_id must be a non-empty string", error_class="validation")
        return self._request("POST", f"/task/{task_id}/cancel")
