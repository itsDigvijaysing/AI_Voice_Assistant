---
name: open-terminal
trigger: user asks to open a terminal, command line, console, shell, or terminal window
tools: [mcp.shell.run_command]
requires: GNOME terminal app. Ptyxis on Ubuntu 26.04; user-level, no sudo
---

Open the terminal via mcp.shell.run_command (no sudo):

- `gtk-launch org.gnome.Ptyxis`   (the GNOME terminal on Ubuntu 26.04)

If that ever fails, try gnome-terminal or xterm instead. Confirm in one short sentence.
