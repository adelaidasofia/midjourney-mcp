# midjourney-mcp

FastMCP server for Midjourney image generation via [PiAPI](https://piapi.ai/),
with a daily-USD cost cap, per-call audit log, and SSRF-safe image URL inputs.

Midjourney has no broadly available official API as of May 2026 (Enterprise
application-only). This MCP wraps PiAPI's PAYG Midjourney surface — the most
stable third-party wrapper at the time of build — behind one consistent tool
surface that fits the same audit + safety patterns as the rest of the family
([cloudflare-dns-mcp](https://github.com/adelaidasofia/cloudflare-dns-mcp),
[godaddy-mcp](https://github.com/adelaidasofia/godaddy-mcp),
[parse-mcp](https://github.com/adelaidasofia/parse-mcp), etc.).

## Why a peer MCP

Other Midjourney MCPs exist (AceDataCloud, z23cc, PiAPI's own TypeScript MCP).
This one ships:

1. **Daily USD cap.** A single env var (`MIDJOURNEY_MCP_DAILY_USD_CAP`) sets a
   hard ceiling on per-day image-gen spend. Cost-incurring tools refuse with a
   stable `error_class="rate_cap"` once the projected day total exceeds cap.
2. **Per-call 4-field audit log** (`execution_time_ms` / `io` / `token_usage` /
   `error_class`) at `~/.claude/midjourney-mcp/audit.log.jsonl`, with actual
   PiAPI credit consume rolled in on completion.
3. **SSRF-safe image URL inputs** on `describe` + `blend` via
   [mycelium-security](https://github.com/adelaidasofia/mycelium-security)
   `sanitize_or_raise` + `assert_public_ip`. Defense in depth — refuses
   private / link-local / cloud metadata service hosts before forwarding.
4. **`sanitize_error()` strip patterns** on every error payload — X-API-Key,
   Bearer tokens, api_key / secret / password / token patterns get redacted
   before crossing into model context.
5. **`admin.env` auto-load.** Secrets live at `~/.claude/midjourney-mcp/admin.env`
   (chmod 600, gitignored), never inline in `.mcp.json`.

## Install

```bash
cd ~/.claude/midjourney-mcp
python3 -m pip install --break-system-packages -r requirements.txt
```

Then drop the API key into `admin.env`:

```bash
printf 'PIAPI_API_KEY=your-piapi-key-here\n' > ~/.claude/midjourney-mcp/admin.env
chmod 600 ~/.claude/midjourney-mcp/admin.env
```

Sign up at <https://piapi.ai/> if you need a key. PAYG starts at ~$0.01 per
imagine task. Host-Your-Account ($8/seat/mo) is also supported — set the
daily cap to 0 (`MIDJOURNEY_MCP_DAILY_USD_CAP=0`) to disable cost tracking in
HYA mode.

Register in your project `.mcp.json`:

```json
{
  "mcpServers": {
    "midjourney": {
      "command": "python3",
      "args": ["/Users/YOU/.claude/midjourney-mcp/server.py"],
      "env": {}
    }
  }
}
```

Then restart Claude Code.

## Tools (11 total)

### Read (no cost, no cap)

| Tool | What it does |
|---|---|
| `healthcheck` | Verify the PiAPI key is set + return today's cap snapshot |
| `account_info` | Today's USD cap + per-tool cost estimates + default process_mode |
| `get_task(task_id)` | Single poll for task status + output URLs |
| `wait_for_task(task_id, timeout_seconds, poll_interval_seconds)` | Block until terminal state |
| `list_recent_tasks(limit)` | Scan the local audit log for recent task_ids |

### Cost-incurring (cap-gated)

| Tool | Estimated USD (fast mode) | What it does |
|---|---:|---|
| `imagine(prompt, aspect_ratio, ...)` | $0.040 | 4-up grid from a text prompt |
| `variation(origin_task_id, index, prompt, ...)` | $0.040 | Regenerate variations off a grid (index 1-4 / high_variation / low_variation) |
| `upscale(origin_task_id, index, ...)` | $0.010 | Isolate + upscale one grid image (index 1-4 / light / beta / 2x / 4x / subtle / creative) |
| `describe(image_url, ...)` | $0.005 | 4 prompts from an input image (image-to-prompt) |
| `blend(image_urls, dimension, ...)` | $0.040 | Merge 2-5 images into a new 4-up grid |

### Lifecycle

| Tool | What it does |
|---|---|
| `cancel_task(task_id)` | Cancel a non-terminal task |

## Daily USD cap

Image generation is creative iteration. Draft+confirm on every call breaks the
loop. Instead, every cost-incurring tool runs a cap check BEFORE the API call:

```
projected = spent_today_usd + estimated_call_usd
if projected > MIDJOURNEY_MCP_DAILY_USD_CAP:
    refuse with error_class="rate_cap"
```

Spent-today tracks the calendar day at a configurable UTC offset (default
`-5`; override via `MIDJOURNEY_MCP_TZ_OFFSET_HOURS=-8` for US Pacific
Standard, `1` for Central European, etc.). Cap resets at midnight in that
offset. Estimates drive the cap (conservative pre-flight); actual PiAPI
credit consume is backfilled from `meta.usage.consume` on task completion
and lands in the audit log.

Default cap: `$5.00/day`. Override via env: `MIDJOURNEY_MCP_DAILY_USD_CAP=20.00`.
Set to `0` to disable (e.g. HYA mode with flat $8/mo billing).

## Typical flow

```python
# 1. Submit an imagine
imagine(prompt="a quiet kitchen at dawn, soft window light, film grain --ar 3:2", aspect_ratio="3:2")
# -> {"task_id": "tk_abc...", "status": "pending", ...}

# 2. Wait for it
wait_for_task(task_id="tk_abc...", timeout_seconds=180)
# -> {"status": "completed", "task": {"output": {"image_url": "...", "image_urls": [...]}}}

# 3. Upscale the best one (say grid position 2)
upscale(origin_task_id="tk_abc...", index="2")
# -> {"task_id": "tk_xyz...", ...}
wait_for_task(task_id="tk_xyz...")

# Or vary instead of upscale:
variation(origin_task_id="tk_abc...", index="3", prompt="<same prompt, tweak>")
```

## Audit log

Every tool call writes one JSONL line at `~/.claude/midjourney-mcp/audit.log.jsonl`:

```json
{
  "ts": 1737842400,
  "tool": "imagine",
  "execution_time_ms": 1230,
  "io": {"input": {"prompt_chars": 47, "aspect_ratio": "3:2"}, "output": {"task_id": "tk_abc...", "status": "pending"}},
  "token_usage": {},
  "error_class": "none",
  "extra": {"task_id": "tk_abc...", "usd_estimate": 0.04}
}
```

Search by tool, date, error_class, task_id. Useful for cost attribution + bug
triage. Override the path via `MIDJOURNEY_MCP_AUDIT_LOG`.

## Process modes

PiAPI translates `process_mode` to Midjourney's plan-level modes:

- `relax` — slowest, cheapest, no GPU minutes consumed on official MJ plans
- `fast` — default, normal-quality GPU time
- `turbo` — fastest, premium GPU time, ~2× cost

Override default via env: `MIDJOURNEY_MCP_PROCESS_MODE=fast` (default).
Override per-call via the `process_mode` argument on any tool.

## Aspect ratios

Pass `aspect_ratio` to `imagine` / `variation`. Common values:

- `1:1` (square, default)
- `3:2`, `2:3` (classic photo)
- `16:9`, `9:16` (cinema / portrait phone)
- `4:3`, `3:4` (older monitor / portrait)
- `21:9` (ultra-wide)

PiAPI also accepts Midjourney's `--ar W:H` flag in the prompt itself; either
works, but `aspect_ratio` is cleaner.

## Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `PIAPI_API_KEY` | (required) | PiAPI API key. Auto-loaded from `admin.env`. |
| `PIAPI_API_BASE` | `https://api.piapi.ai/api/v1` | Override for mocking / testing. |
| `MIDJOURNEY_MCP_DAILY_USD_CAP` | `5.00` | Daily USD spend cap. `0` disables. |
| `MIDJOURNEY_MCP_PROCESS_MODE` | `fast` | Default mode if not specified per-call. |
| `MIDJOURNEY_MCP_AUDIT_LOG` | `~/.claude/midjourney-mcp/audit.log.jsonl` | Audit log path. |
| `MIDJOURNEY_MCP_SPEND_FILE` | `~/.claude/midjourney-mcp/spend.json` | Daily spend tracker path. |

## Related MCPs

Same author, same install path (`~/.claude/<name>-mcp`), same safety patterns:

- [whatsapp-mcp](https://github.com/adelaidasofia/whatsapp-mcp) — WhatsApp Web bridge with draft+confirm sends
- [imessage-mcp](https://github.com/adelaidasofia/imessage-mcp) — iMessage with Whisper voice transcription
- [slack-mcp](https://github.com/adelaidasofia/slack-mcp) — Multi-workspace Slack with draft+confirm
- [substack-mcp](https://github.com/adelaidasofia/substack-mcp) — Notes + drafts + post management
- [parse-mcp](https://github.com/adelaidasofia/parse-mcp) — Multi-backend document parser
- [godaddy-mcp](https://github.com/adelaidasofia/godaddy-mcp) — GoDaddy DNS with draft+confirm
- [cloudflare-dns-mcp](https://github.com/adelaidasofia/cloudflare-dns-mcp) — Cloudflare DNS with draft+confirm
- [finance-mcp](https://github.com/adelaidasofia/finance-mcp) — Plaid-backed personal finance

## License

MIT.

---

Built by Adelaida Diaz-Roa. Full install or team version at diazroa.com.
