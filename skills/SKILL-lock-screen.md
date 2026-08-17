---
name: lock-screen
trigger: user asks to lock the screen or lock the computer
tools: [mcp.skills_actions.lock_screen]
requires: systemd (loginctl), present on Ubuntu 26.04; runs as the user, no sudo
---

Call `mcp.skills_actions.lock_screen` (it runs `loginctl lock-session`).

Locking ONLY, by policy (commit 745f20e) the assistant never suspends, sleeps,
logs out, or powers off the machine; those commands were removed from the tool
surface. "Go to sleep" is the separate `go_to_sleep` built-in, which puts the
ASSISTANT to sleep (ends the wake-word conversation window), never the OS.
