---
name: open-file-manager
trigger: user asks to open the file manager, files, open a folder, show my home / downloads / documents / pictures folder, or browse files
tools: [mcp.shell.run_command]
requires: xdg-utils + Nautilus (GNOME Files), present on Ubuntu 26.04; user-level, no sudo
---

Open a folder via mcp.shell.run_command (no sudo):

- Home folder:        `xdg-open "$HOME"`
- A specific folder:  `xdg-open "$HOME/Downloads"`   (also Documents, Pictures, Desktop, Music, Videos)
- The Files app:      `gtk-launch org.gnome.Nautilus`

Resolve the folder the user named under `$HOME` (e.g. "downloads" -> `$HOME/Downloads`). Quote the path. Confirm what you opened in one short sentence.
