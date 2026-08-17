---
name: system-status
trigger: user asks how the computer is doing, cpu, memory, temperature, uptime
tools: [mcp.system_info.*, mcp.time_info.*]
---

1. Call `mcp.system_info.system_overview` (or `cpu_load` / `memory_usage` / `temperatures`).
2. For uptime, call `mcp.time_info.uptime_seconds`.
3. Summarize in one or two short sentences. Never read raw JSON aloud.
