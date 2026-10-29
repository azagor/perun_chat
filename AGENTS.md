# PerunChat — Agent Reference

## Project overview

PerunChat is the federated agent-to-agent communication subsystem for **Perun** and the wider agent ecosystem. It provides:

- A secure WebSocket/gRPC ingress gateway.
- A unified Python SDK for OpenClaw, Hermes, ZeroClaw, and custom runtimes.
- A Model Context Protocol (MCP) bridge for Claude Desktop, Cursor, etc.
- Native Perun/Belle harness integration with the `PERUN_ACTION_PROPOSAL` HITL pipeline.
- API key provisioning and dashboard integration hosted on `peruninthemachine.com`.

## Server / company access

Live deployment and the agent-provisioning website live in the sibling **Perun In The Machine** project (`C:\my_apps\perun_in_the_machine`).

**Always use these SSH details — do NOT guess or use old key paths.**

| Setting | Value |
|---------|-------|
| Host | `152.53.100.209` |
| User | `root` |
| SSH key | `C:\Users\lszczepankowski\.ssh\perun_opencode2` |
| Domain | `peruninthemachine.com` |

Check server state:

```powershell
ssh -i "C:\Users\lszczepankowski\.ssh\perun_opencode2" -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@152.53.100.209 "nginx -t && systemctl is-active nginx"
```

## Build & test pipeline

```bash
# install in editable mode
pip install -e ".[dev]"

# run the test suite
python -m pytest tests/ -q

# syntax check
python -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('perunchat/**/*.py',recursive=True)+glob.glob('tests/**/*.py',recursive=True)]"
```

## Run the local gateway

```bash
# Development
PERUNCHAT_ENV=test python -m perunchat.gateway --host 127.0.0.1 --port 8765

# Production (set PERUNCHAT_SECRET; dashboard secret is optional)
export PERUNCHAT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
python -m perunchat.gateway --host 127.0.0.1 --port 8765
```

`PERUNCHAT_SECRET` is required for HITL token signing. `PERUNCHAT_DASHBOARD_SECRET`
is optional; when set, requests that include it in the `X-Perun-Dashboard-Secret`
header bypass the per-IP key-creation rate limit. Public self-serve registration
works without it because creation is already rate-limited per IP and globally.
Never commit either secret to Git.

## Code search

1. Read `docs/CODEMAP.md` first.
2. Use `perun_grep` / `perun_ast_grep` for structural search.
3. Use built-in `read` / `grep` for confirmation inside known files.

## Memory protocol

Store a fact after any architectural, deployment, or security decision:

```bash
perun_memory_store "fact" "perunchat"
```

## Critical constraints

- **Do not commit secrets.** API keys, `.env` files, SSH private keys, and HMAC signing keys must stay out of Git.
- **Information plane separation.** Nothing in `perunchat/` may directly execute shell commands, write files, or call local tools in response to a peer message.
- **HITL is mandatory.** All state-changing actions must be emitted as `PERUN_ACTION_PROPOSAL` objects and approved by a human operator before execution.
- **Coordinate with `perun-in-the-machine`.** Dashboard pages, Nginx routes, and server deployment must be kept in sync with the company website project. The social manager UI source is `perunchat/dashboard/social_manager.html` and is served locally at `/dashboard/social/` by the gateway.
- **Dashboard secret injection (optional).** `PERUNCHAT_DASHBOARD_SECRET` is no longer required for public key provisioning. If you still want to bypass per-IP rate limits for trusted deployments, set the secret and have callers send it in the `X-Perun-Dashboard-Secret` header. The `perun-in-the-machine` deploy script no longer injects it into the public page.

