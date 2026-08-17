---
name: take-screenshot
trigger: user asks to take a screenshot, grab the screen, capture the screen, screenshot a window or area, or snap a picture of the screen
tools: [mcp.shell.run_command]
requires: ai-linux-screenshot + computer-use-linux (installed by `./ai-linux setup`); screen-capture permission is granted once during setup.
---

Take a screenshot via mcp.shell.run_command using `ai-linux-screenshot`. It captures through GNOME's sanctioned screen-capture portal (the only way an app may read the screen on Wayland) and saves a PNG to ~/Pictures. Do NOT use gnome-screenshot, it is not installed and does not work on Wayland.

- Whole screen: `ai-linux-screenshot --full`
- A specific window: `ai-linux-screenshot --window "<window title>"`

The command prints the saved file path. Tell the user where it was saved in one short sentence. If it prints that capture isn't permitted, say screen capture isn't set up yet and that `./ai-linux setup` grants it.
