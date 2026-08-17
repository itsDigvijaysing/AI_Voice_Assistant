---
name: clipboard-copy-paste
trigger: user asks to copy something to the clipboard, put text on the clipboard, paste, or read what is in the clipboard
tools: [mcp.shell.run_command]
requires: wl-clipboard (wl-copy/wl-paste), installed by `./ai-linux setup`. If missing, say so.
---

Use the Wayland clipboard via mcp.shell.run_command. If `command -v wl-copy` is empty, tell the user it is not installed (run `./ai-linux setup`) and don't pretend it worked.

- Copy text to clipboard: `wl-copy "the text to copy"`
- Read the clipboard:     `wl-paste`

Confirm in one short sentence (e.g. "Copied to your clipboard.").
