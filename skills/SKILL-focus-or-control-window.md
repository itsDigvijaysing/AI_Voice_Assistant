---
name: focus-or-control-window
trigger: user asks to switch to / focus / activate / raise an existing window, or to click, type, or scroll inside an on-screen app (GUI automation)
tools: [mcp.computer_use.*]
requires: computer_use MCP server enabled (Phase 3); all actions are confirm-gated
---

Use this for interacting with apps that are ALREADY running on screen. To start an app or go to a
link / file / folder from scratch, prefer the launch skill (it is simpler than GUI automation).

1. `mcp.computer_use.list_windows` / `list_apps` to find the target window.
2. `activate_window` to focus / raise it; then `click`, `type_text`, `press_key`, or `scroll` to operate it.
3. Each action passes the confirm-before-execute safety gate. If denied, stop and say so, don't retry silently.
