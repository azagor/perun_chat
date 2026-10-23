# PerunChat - Run the local gateway

Extracted verbatim from `AGENTS.md` as part of the context-budget migration. Retrievable as `perunchat.context.gateway`.

## Run the local gateway

```bash
# Development
PERUNCHAT_ENV=test python -m perunchat.gateway --host 127.0.0.1 --port 8765

# Production (set both secrets first)
export PERUNCHAT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
export PERUNCHAT_DASHBOARD_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
python -m perunchat.gateway --host 127.0.0.1 --port 8765
```

The `PERUNCHAT_DASHBOARD_SECRET` is required so the public agent dashboard at
`peruninthemachine.com/perunchat/` can provision keys. The company website
injects this secret at deploy time; never commit it to Git.
