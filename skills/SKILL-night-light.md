---
name: toggle-night-light
trigger: user asks to turn on or off night light, blue light filter, warm screen, night mode, or make the screen warmer or cooler
tools: [mcp.shell.run_command]
requires: GNOME settings-daemon color plugin (gsettings), present on Ubuntu 26.04; user-level, no sudo
---

Control GNOME Night Light by running ONE command via mcp.shell.run_command (no sudo):

- Turn ON:   `gsettings set org.gnome.settings-daemon.plugins.color night-light-enabled true`
- Turn OFF:  `gsettings set org.gnome.settings-daemon.plugins.color night-light-enabled false`
- Warmer:    `gsettings set org.gnome.settings-daemon.plugins.color night-light-temperature 3500`   (lower = warmer, ~2700–6500)
- Cooler:    `gsettings set org.gnome.settings-daemon.plugins.color night-light-temperature 5500`
- Check:     `gsettings get org.gnome.settings-daemon.plugins.color night-light-enabled`

Confirm in one short sentence.
