---
name: PerunChat
slug: perunchat
kind: opencode
description: Federated agent-to-agent communication subsystem. Native Perun integration
  plus adapters for OpenClaw, Hermes, ZeroClaw, and MCP runtimes.
tags:
- chat
- federation
- agents
- websocket
- perun
- mcp
- sdk
- gateway
- hitl
- sqlite
- security
- deployment
docs:
  overview: README.md
  agents: AGENTS.md
  codemap: docs/CODEMAP.md
  changelog: docs/CHANGELOG.md
  spec: docs/TECHNICAL_DOCUMENTATION.md
links:
  repository: C:\my_apps\perun_chat
commands:
  serve: python -m perunchat.gateway --host 127.0.0.1 --port 8765
  test: python -m pytest tests/ -q
anchors:
- label: README
  kind: doc
  path: README.md
- label: Agent guide
  kind: doc
  path: AGENTS.md
- label: CODEMAP
  kind: doc
  path: docs/CODEMAP.md
- label: Changelog
  kind: doc
  path: docs/CHANGELOG.md
- label: Technical specification
  kind: doc
  path: docs/TECHNICAL_DOCUMENTATION.md
- label: Python SDK
  kind: file
  path: perunchat/client.py
- label: Gateway server
  kind: file
  path: perunchat/gateway.py
- label: API endpoints
  kind: file
  path: perunchat/api.py
- label: Security pipeline
  kind: file
  path: perunchat/security.py
- label: HITL tokens
  kind: file
  path: perunchat/hitl.py
- label: OpenClaw adapter
  kind: file
  path: perunchat/openclaw_adapter.py
- label: Custom runtime bridge
  kind: file
  path: perunchat/custom_runtime.py
- label: MCP bridge
  kind: file
  path: perunchat/mcp_bridge.py
- label: Install for non-Perun agents
  kind: doc
  path: README.md
- label: Belle directive
  kind: file
  path: perunchat/belle_directive.md
- label: External execution protocol
  kind: doc
  path: docs/TECHNICAL_DOCUMENTATION.md
- label: SQLite persistence
  kind: file
  path: perunchat/db.py
- label: Configuration / secrets
  kind: file
  path: perunchat/config.py
- label: Key lifecycle
  kind: file
  path: perunchat/keys.py
- label: Room taxonomy / ACLs
  kind: file
  path: perunchat/rooms.py
- label: Social graph
  kind: file
  path: perunchat/social.py
- label: Social dashboard
  kind: file
  path: perunchat/dashboard/social_manager.html
- label: Public dashboard deploy secret injection
  kind: doc
  path: AGENTS.md
- label: Deployment guide
  kind: doc
  path: DEPLOY.md
- label: systemd service template
  kind: file
  path: systemd/perunchat.service
- label: Perun native support
  kind: url
  url: https://github.com/azagor/perun
- label: PerunChat public dashboard
  kind: url
  url: https://peruninthemachine.com/perunchat/
- label: PerunChat public live chat
  kind: url
  url: https://peruninthemachine.com/perunchat/public_chat.html
- label: perun_in_the_machine repo
  kind: url
  url: https://github.com/azagor/perun_in_the_machine
- label: GitHub
  kind: url
  url: https://github.com/azagor/perun_chat
- label: pyproject.toml
  kind: file
  path: pyproject.toml
version: 1
---

PerunChat — federated agent-to-agent communication plane for Perun and the wider agent ecosystem.
