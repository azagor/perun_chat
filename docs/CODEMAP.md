# PerunChat — CODEMAP

Navigation source of truth. Start every lookup here.

| Subsystem | Files | Key identifiers | Documentation |
|-----------|-------|-----------------|---------------|
| Project manifest | `perun.md` | `perunchat` slug, docs, commands, anchors | `perun.md` |
| Agent reference | `AGENTS.md` | build/test commands, server access constraints | `AGENTS.md` |
| User overview | `README.md` | quick start, project layout | `README.md` |
| Agent integration | `docs/AGENT_INTEGRATION.md` | non-Perun agent wire protocol, two-phase HITL, examples | `docs/AGENT_INTEGRATION.md` |
| Technical spec | `docs/TECHNICAL_DOCUMENTATION.md` | architecture, wire protocol, security model, HITL | `docs/TECHNICAL_DOCUMENTATION.md` |
| Changelog | `docs/CHANGELOG.md` | release history | `docs/CHANGELOG.md` |
| Python SDK | `perunchat/client.py` | `PerunChatClient`, `RoomType`, `RiskTier`, `reconnect`, self-echo filter, `stage_proposal()`, `resubmit_approved_proposal()` | §7.1 spec, `README.md`, `docs/AGENT_INTEGRATION.md` |
| Gateway server | `perunchat/gateway.py` | `app`, `ChatGateway`, `handle_ws`, `_effective_runtime`, `MAX_FRAME_BYTES`, `_is_managed_project_room`, proposal staging/validation, echo/replay preservation | §2 spec, `docs/TECHNICAL_DOCUMENTATION.md` |
| API endpoints | `perunchat/api.py` | `router`, key endpoints, `/api/v1/me`, `/api/v1/rooms`, `/api/v1/proposals`, `ProposalStore`, `ProposalStoreFull`, atomic `mark_approved()` | §4/5 spec |
| Security pipeline | `perunchat/security.py` | `SecurityPipeline`, `DLPScanner`, `RateLimiter`, `AuthRateLimiter`, `SequenceValidator`, `TurnCounter` (per-agent keys, resets on operator/custom/dashboard/human runtimes, prunes stale entries), `pre_validate()` / `commit_turn()` split | §3/5 spec |
| HITL tokens | `perunchat/hitl.py` | `HITLTokenEngine`, `ApprovalStore` (SQLite-persisted nonces, fail-closed `max_consumed_nonces`), `ApprovalStoreBusy`, `ApprovalStoreFull`, `action_hash()` | §5.5/5.7 spec |
| Key lifecycle | `perunchat/keys.py` | `KeyStore` (SQLite-backed, in-memory tests) | §4 spec |
| Persistence | `perunchat/db.py` | `db()` context manager, `init_db()`, WAL mode, `pending_proposals` schema | §8 spec |
| Configuration | `perunchat/config.py` | `secret_key()`, `dashboard_secret()`, `db_path()`, `public_rooms()` (`PERUNCHAT_PUBLIC_ROOMS`), `max_consumed_nonces()` (`PERUNCHAT_MAX_CONSUMED_NONCES`), env loading | §8 spec |
| Room taxonomy | `perunchat/rooms.py` | `RoomResolver`, `private_room_id`, `parse_room_id()` (rejects non-strings), `can_join()`, `room_managed_by()`, case-insensitive IDs | §2.1/5.3 spec |
| Social graph | `perunchat/social.py` | `SocialGraph`, `SocialGraphError` | §5.4 spec |
| Social dashboard | `perunchat/dashboard/social_manager.html` | `/dashboard/social/` | README |
| Models | `perunchat/models.py` | `ChatFrame`, `ActionProposal`, `AgentKey` | §2/4/5 spec |
| Hermes adapter | `perunchat/hermes_adapter.py` | `HERMES_PERUNCHAT_TOOLS`, `execute_hermes_tool` | §7.2 spec, `docs/AGENT_INTEGRATION.md` |
| ZeroClaw micro-client | `perunchat/zeroclaw_client.py` | `ZeroClawClient`, `connect_zeroclaw` | §7.3 spec, `docs/AGENT_INTEGRATION.md` |
| OpenClaw adapter | `perunchat/openclaw_adapter.py` | `PerunChatOpenClawPlugin`, `WebSocketTransport`, `make_openclaw_plugin` | §7 spec, `docs/AGENT_INTEGRATION.md` |
| Custom runtime bridge | `perunchat/custom_runtime.py` | `PerunChatRuntime`, `Transport` | §7 spec |
| MCP bridge | `perunchat/mcp_bridge.py` | `serve_mcp`, `PerunChatMCPServer`, persistent client, resource reader, Perun handoff tools (`claim`, `download`, `upload`, `write_target_file`, `status`, `revoke`) | §7.5 spec, `docs/AGENT_INTEGRATION.md` |
| Belle directive | `perunchat/belle_directive.md` | `PERUN_ACTION_PROPOSAL` format + external execution handoff | §6 spec, §6.1 external execution |
| Deployment | `DEPLOY.md`, `systemd/perunchat.service` | install steps, `EnvironmentFile=/etc/perunchat.env`, Nginx `/chat/` + `/v1/chat/ws` routes, live VPS facts | `DEPLOY.md` |
| Test suite | `tests/` | 14 files, 244 tests: `test_api.py`, `test_gateway.py`, `test_hitl.py`, `test_client.py`, `test_rooms.py`, `test_social.py`, `test_keys.py`, `test_security.py`, `test_models.py`, `test_mcp_bridge.py`, `test_adapter_conformance.py`, `test_custom_runtime.py`, `test_openclaw_adapter.py`, `test_zeroclaw.py` | `README.md` |
| Package init | `perunchat/__init__.py`, `perunchat/_version.py` | exports, `__version__` (0.4.2) | `README.md` |
| Packaging | `pyproject.toml` | dependencies, scripts, optional extras (`mcp`, `zeroclaw`, `dev`) | `README.md` |

## Cross-project relationships

- `perun` (`C:\my_apps\perun`) — the parent orchestrator. PerunChat adds native chat support to Perun-managed agents.
- `perun-in-the-machine` (`C:\my_apps\perun_in_the_machine`) — company website and live VPS hosting. Coordinates dashboard UI, Nginx routes, and server deployment.
- `trading_bot` (`C:\my_apps\trading_bot`) — sibling product with backend-service patterns; consult for FastAPI/Nginx deployment conventions.

## Live production (since 2026-10-19)

- Gateway: `perunchat` systemd service on the company VPS, code at `/opt/perun_chat`, secrets in `/etc/perunchat.env`, state in `/var/lib/perunchat/perunchat.db`.
- Public endpoints: REST `https://peruninthemachine.com/chat/api/v1/*`, WebSocket `wss://peruninthemachine.com/v1/chat/ws`, dashboard `https://peruninthemachine.com/perunchat/`.
