---
name: do-not-disturb
trigger: user asks to turn on or off do not disturb, silence notifications, mute notifications, hide notification banners, or focus mode
tools: [mcp.shell.run_command]
requires: GNOME (gsettings), present on Ubuntu 26.04; user-level, no sudo
---

Toggle GNOME notification banners (Do Not Disturb) via mcp.shell.run_command (no sudo):

- Do Not Disturb ON (silence):  `gsettings set org.gnome.desktop.notifications show-banners false`
- Do Not Disturb OFF (allow):   `gsettings set org.gnome.desktop.notifications show-banners true`
- Check:                        `gsettings get org.gnome.desktop.notifications show-banners`

Note: `show-banners false` = DND on. Confirm in one short sentence.
