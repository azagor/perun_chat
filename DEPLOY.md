# PerunChat Deployment Notes

> For the full company-website + VPS context, see the sibling
> `perun-in-the-machine/company_site/DEPLOY.md`.

## Live production (since 2026-10-19)

The gateway runs on the company VPS as the `perunchat` systemd service:

| Item | Value |
|------|-------|
| Code | `/opt/perun_chat` (cloned from this repo, `main`) |
| Service | `perunchat.service` — enabled, restart on failure |
| Secrets | `/etc/perunchat.env` (mode 600, sourced via `EnvironmentFile=`) |
| State | `/var/lib/perunchat/perunchat.db` |
| Public REST | `https://peruninthemachine.com/chat/api/v1/*` (Nginx `/chat/`) |
| Public WS | `wss://peruninthemachine.com/v1/chat/ws` |
| Dashboard | `https://peruninthemachine.com/perunchat/` |

Update procedure after pushing to `main`:

The working deployment key from this machine is `~/.ssh/perun_opencode2` for
`root@152.53.100.209` (the host resolved from `peruninthemachine.com`).

```bash
# from the local development machine
ssh -i ~/.ssh/perun_opencode2 root@152.53.100.209 \
  "cd /opt/perun_chat && git pull && systemctl restart perunchat && sleep 2 && curl -s http://127.0.0.1:8765/health"
```

Or, logged in directly on the VPS:

```bash
cd /opt/perun_chat && git pull && systemctl restart perunchat
curl -s http://127.0.0.1:8765/health
```

## Prerequisites

- A Linux host with Python 3.10+ and `systemd`.
- Nginx (or another reverse proxy) terminating TLS and proxying `/chat/` and
  `/v1/chat/ws` to the gateway.
- A 256-bit HMAC secret for signing HITL approval tokens.

## Install

```bash
# 1. Clone / checkout the code
sudo mkdir -p /opt/perun_chat
sudo chown $USER:$USER /opt/perun_chat
git clone https://github.com/azagor/perun_chat.git /opt/perun_chat
cd /opt/perun_chat

# 2. Create virtual environment and install
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 3. Create state directories
sudo mkdir -p /var/lib/perunchat
sudo useradd -r -s /bin/false perunchat || true
sudo chown -R perunchat:perunchat /var/lib/perunchat

# 4. Generate and set the HMAC secret (dashboard secret is optional)
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
DASHBOARD_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
{
    echo "PERUNCHAT_SECRET=$SECRET"
    echo "PERUNCHAT_DASHBOARD_SECRET=$DASHBOARD_SECRET"
} | sudo tee /etc/perunchat.env
# PERUNCHAT_DASHBOARD_SECRET is optional; omit it if you don't need to bypass the
# per-IP key-creation rate limit for trusted dashboard deployments.
# Restrict permissions (the systemd unit sources this file via EnvironmentFile)
sudo chmod 600 /etc/perunchat.env
sudo chown perunchat:perunchat /etc/perunchat.env
```

## systemd service

```bash
# The unit sources its secrets from /etc/perunchat.env (EnvironmentFile).
sudo cp systemd/perunchat.service /etc/systemd/system/perunchat.service

sudo systemctl daemon-reload
sudo systemctl enable --now perunchat
```

## Nginx

The company website Nginx config already contains the required `/chat/` and
`/v1/chat/ws` proxy blocks. After deploying this service, reload Nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## Verify

```bash
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
sudo journalctl -u perunchat -f
```

Public smoke checks (through Nginx):

```bash
# REST health via the /chat/ route
curl -s https://peruninthemachine.com/chat/health
# WebSocket upgrade through TLS (expect HTTP/1.1 101)
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  https://peruninthemachine.com/v1/chat/ws
# Dashboard page (expect 200)
curl -s -o /dev/null -w '%{http_code}\n' https://peruninthemachine.com/perunchat/
```

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `PERUNCHAT_SECRET` | yes | — | 64-char hex HMAC key for approval tokens |
| `PERUNCHAT_DASHBOARD_SECRET` | no | — | Optional shared secret that, when sent in `X-Perun-Dashboard-Secret`, bypasses the per-IP key-creation rate limit. |
| `PERUNCHAT_DB_PATH` | no | `~/.perunchat/perunchat.db` | SQLite database path |
| `PERUNCHAT_MAX_CONSUMED_NONCES` | no | `100000` | Fail-closed cap for the append-only consumed-nonce table |
| `PERUNCHAT_HOST` | no | `127.0.0.1` | Gateway bind address |
| `PERUNCHAT_PORT` | no | `8765` | Gateway bind port |
| `PERUNCHAT_ENV` | no | — | Set to `test` for unit-test safe defaults |

### Reclaiming consumed-nonce space

Approval tokens never expire, so consumed nonces cannot be pruned by time:
deleting a nonce would let its still-valid token be replayed. The store fails
closed at `PERUNCHAT_MAX_CONSUMED_NONCES`. To reclaim space safely:

1. Rotate `PERUNCHAT_SECRET` so every outstanding token becomes invalid.
2. Restart the gateway.
3. Prune the table explicitly, e.g.:

   ```sql
   DELETE FROM consumed_nonces WHERE used_at < strftime('%s','now') - 31536000;
   ```

Never prune while old tokens may still be in circulation.
