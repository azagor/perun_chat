# PerunChat — CODEMAP

Navigation source of truth. Start every lookup here.

| Subsystem | Files | Key identifiers | Documentation |
|-----------|-------|-----------------|---------------|
| Project manifest | `perun.md` | `perunchat` slug, docs, commands, anchors | `perun.md` |
| Agent reference | `AGENTS.md` | build/test commands, server access constraints | `AGENTS.md` |
| User overview | `README.md` | quick start, project layout | `README.md` |
| Technical spec | `docs/TECHNICAL_DOCUMENTATION.md` | wire protocol, security model, HITL | `docs/TECHNICAL_DOCUMENTATION.md` |
| Changelog | `docs/CHANGELOG.md` | release history | `docs/CHANGELOG.md` |
| Python SDK | `perunchat/client.py` | `PerunChatClient`, `RoomType`, `RiskTier` | §7.1 spec, `README.md` |
| Gateway server | `perunchat/gateway.py` | `app`, `ChatGateway`, `handle_ws`, proposal cleanup | §2 spec, `docs/TECHNICAL_DOCUMENTATION.md` |
| API endpoints | `perunchat/api.py` | `router`, key endpoints, `/api/v1/me`, `/api/v1/rooms` (room discovery: persisted memberships + active rooms + `public_rooms()`), availability check | §4/5 spec |
| Security pipeline | `perunchat/security.py` | `SecurityPipeline`, `DLPScanner`, `RateLimiter`, `AuthRateLimiter` | §3 spec |
| HITL tokens | `perunchat/hitl.py` | `HITLTokenEngine`, `ApprovalStore` | §5.7 spec |
| Key lifecycle | `perunchat/keys.py` | `KeyStore` (SQLite-backed, in-memory tests) | §4 spec |
| Persistence | `perunchat/db.py` | `db()` context manager, `init_db()`, WAL mode | §8 spec |
| Configuration | `perunchat/config.py` | `secret_key()`, `dashboard_secret()`, `db_path()`, `public_rooms()` (`PERUNCHAT_PUBLIC_ROOMS`), env loading | §8 spec |
| Room taxonomy | `perunchat/rooms.py` | `RoomResolver`, `private_room_id`, `can_join()`, case-insensitive IDs | §2.1/5.3 spec |
| Social graph | `perunchat/social.py` | `SocialGraph`, `SocialGraphError` | §5.4 spec |
| Social dashboard | `perunchat/dashboard/social_manager.html` | `/dashboard/social/` | README |
| Models | `perunchat/models.py` | `ChatFrame`, `ActionProposal`, `AgentKey` | §2/4/5 spec |
| Hermes adapter | `perunchat/hermes_adapter.py` | `HERMES_PERUNCHAT_TOOLS` | §7.2 spec |
| ZeroClaw micro-client | `perunchat/zeroclaw_client.py` | `connect_zeroclaw` | §7.3 spec |
| OpenClaw adapter | `perunchat/openclaw_adapter.py` | `PerunChatOpenClawPlugin`, `make_openclaw_plugin` | §7 spec |
| Custom runtime bridge | `perunchat/custom_runtime.py` | `PerunChatRuntime`, `Transport` | §7 spec |
| MCP bridge | `perunchat/mcp_bridge.py` | `serve_mcp`, `PerunChatMCPServer` | §7.4 spec |
| Belle directive | `perunchat/belle_directive.md` | `PERUN_ACTION_PROPOSAL` format | §6 spec |
| Package init | `perunchat/__init__.py` | exports | `README.md` |
| Packaging | `pyproject.toml` | dependencies, scripts | `README.md` |

## Cross-project relationships

- `perun` (`C:\my_apps\perun`) — the parent orchestrator. PerunChat adds native chat support to Perun-managed agents.
- `perun-in-the-machine` (`C:\my_apps\perun_in_the_machine`) — company website and live VPS hosting. Coordinates dashboard UI, Nginx routes, and server deployment.
- `trading_bot` (`C:\my_apps\trading_bot`) — sibling product with backend-service patterns; consult for FastAPI/Nginx deployment conventions.
