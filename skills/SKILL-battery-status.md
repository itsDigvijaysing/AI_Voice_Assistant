---
name: battery-status
trigger: user asks about the battery, power level, charge, how much battery is left, or whether it is charging
tools: [mcp.shell.run_command]
requires: upower, present on Ubuntu 26.04; user-level, no sudo
---

Check battery via mcp.shell.run_command (no sudo), then tell the user plainly:

- `upower -i $(upower -e | grep BAT) | grep -E "state|percentage|time"`
- Quick percentage only: `cat /sys/class/power_supply/BAT*/capacity`
- Charging or not:        `cat /sys/class/power_supply/BAT*/status`

Summarize in one short sentence (e.g. "Battery is at 72% and charging.").
