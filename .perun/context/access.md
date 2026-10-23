# PerunChat - Server / company access

Extracted verbatim from `AGENTS.md` as part of the context-budget migration. Retrievable as `perunchat.context.access`.

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
