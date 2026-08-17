---
name: open-system-settings
trigger: user asks to open settings, system settings, preferences, control panel, or a specific panel like sound, display, wifi, network, bluetooth, power, or privacy settings
tools: [mcp.shell.run_command]
requires: gnome-control-center, present on Ubuntu 26.04; user-level, no sudo
---

Open GNOME Settings (optionally a specific panel) via mcp.shell.run_command (no sudo):

- All settings:   `gnome-control-center`
- Sound:          `gnome-control-center sound`
- Display:        `gnome-control-center display`
- Wi-Fi:          `gnome-control-center wifi`
- Network:        `gnome-control-center network`
- Bluetooth:      `gnome-control-center bluetooth`
- Power/battery:  `gnome-control-center power`
- Notifications:  `gnome-control-center notifications`

Pick the panel the user named; if unsure, open `gnome-control-center` with no panel. Confirm in one short sentence.
