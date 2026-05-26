# midjourney-mcp setup

## 1. Sign up for PiAPI

Visit <https://piapi.ai/>, create an account, top up the PAYG balance (or pay
for HYA at $8/seat/mo if you want to host your own Midjourney account).

PAYG is the easier path: ~$0.01 per imagine task, no Midjourney subscription
required on your side, PiAPI handles everything.

Once signed in, go to the API Keys section in the dashboard and copy your key.

## 2. Drop the key into `admin.env`

```bash
mkdir -p ~/.claude/midjourney-mcp
printf 'PIAPI_API_KEY=your-piapi-key-here\n' > ~/.claude/midjourney-mcp/admin.env
chmod 600 ~/.claude/midjourney-mcp/admin.env
```

The client auto-loads `admin.env` at startup, so the `.mcp.json` env block
stays empty. No secrets in `.mcp.json` ever — canonical credential-storage
pattern.

## 3. Install deps

```bash
cd ~/.claude/midjourney-mcp
python3 -m pip install --break-system-packages -r requirements.txt
python3 -c "import server" && echo "OK: midjourney-mcp imports cleanly"
```

The mycelium-security dep installs from GitHub via direct reference and ships
the SSRF guard used on every `describe` / `blend` URL input.

## 4. Register in `.mcp.json`

Edit your project `.mcp.json` (or `~/.claude.json` for user-scope) and add:

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

Validate the JSON:

```bash
python3 -c "import json; json.load(open('.mcp.json'))" && echo "OK"
```

## 5. Restart Claude Code

The 11 tools (`mcp__midjourney__imagine`, `mcp__midjourney__upscale`, etc.)
appear after restart.

## 6. Smoke test

In a fresh Claude Code session inside the project:

```
> use the midjourney mcp healthcheck
```

Should return `ok: True` + a cap snapshot. If `api_key_present` is False, the
key isn't loading from `admin.env`. Verify file location + permissions.

Then try a real generation:

```
> mcp__midjourney__imagine: generate "a quiet kitchen at dawn, soft window light, film grain" with aspect_ratio="3:2"
> mcp__midjourney__wait_for_task: with the returned task_id
```

The task usually completes in 30-90 seconds at `fast` mode.

## 7. Configure the daily cap

Default is $5.00/day. To change:

```bash
# In admin.env (recommended; takes effect on next Claude Code restart)
echo 'MIDJOURNEY_MCP_DAILY_USD_CAP=10.00' >> ~/.claude/midjourney-mcp/admin.env

# Or inline in .mcp.json env block:
# "env": {"MIDJOURNEY_MCP_DAILY_USD_CAP": "10.00"}

# Disable cap entirely (HYA mode):
# MIDJOURNEY_MCP_DAILY_USD_CAP=0
```

## 8. Configure process mode

Default `fast`. Switch to `relax` for cheaper/slower or `turbo` for premium:

```bash
echo 'MIDJOURNEY_MCP_PROCESS_MODE=relax' >> ~/.claude/midjourney-mcp/admin.env
```

Or pass per-call: `imagine(prompt=..., process_mode="turbo")`.

## Troubleshooting

**`api_key_present: false` from healthcheck.** The key isn't loading. Verify
`~/.claude/midjourney-mcp/admin.env` exists, has `PIAPI_API_KEY=...`, and is
chmod 600. Re-import: `cd ~/.claude/midjourney-mcp && python3 -c "import client; c = client.PiAPIClient(); print('key loaded')"`.

**`error_class: auth` on real calls.** The key is loading but PiAPI rejects
it. Sign in to <https://piapi.ai/>, regenerate the key, drop the new value
into `admin.env`, restart Claude Code.

**`error_class: rate_cap` on every cost-incurring tool.** Daily cap reached.
Check today's spend: `account_info()` returns the snapshot. Either raise
`MIDJOURNEY_MCP_DAILY_USD_CAP` and restart, or wait until midnight at your
configured `MIDJOURNEY_MCP_TZ_OFFSET_HOURS` (default `-5`).

**Task hangs at `pending` / `staged`.** PiAPI queueing — the API is up but
no Midjourney slot is free yet. `relax` mode is most prone to this. Either
switch to `fast`/`turbo` for that call or extend `timeout_seconds` on
`wait_for_task`.

**`refused image URL (SSRF guard)` on describe/blend.** The URL points at a
private / link-local / cloud-metadata host. Use a public CDN URL (Imgur,
Cloudflare R2, a public S3 bucket, etc.). The guard is intentional defense
in depth.

**Tools don't appear in Claude Code after restart.** Validate the .mcp.json
parses: `python3 -c "import json; json.load(open('.mcp.json'))"`. If that
passes, check the FastMCP startup log: `python3 ~/.claude/midjourney-mcp/server.py`
should print the MCP banner without error. Common cause: stale pyc cache;
`rm -rf ~/.claude/midjourney-mcp/__pycache__` and restart.
