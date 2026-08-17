---
name: change-screen-brightness
trigger: user asks to change / raise / lower / set the screen or display brightness, or to dim or brighten the screen
tools: [mcp.shell.run_command]
requires: brightnessctl, installed by `./ai-linux setup` (ships a udev rule so a normal user can set brightness; the raw backlight is otherwise root-only). If missing, say so.
---

Set screen brightness via `mcp.shell.run_command`. If `command -v brightnessctl` is empty, tell the user it is not installed (run `./ai-linux setup`) and don't claim you changed brightness.

- Set a level:  `brightnessctl set 50%`   (use the percent the user asked for)
- Brighter:     `brightnessctl set 10%+`
- Dimmer:       `brightnessctl set 10%-`
- Check:        `brightnessctl get` / `brightnessctl max`

(Keyboard backlight, if asked, is separate: `brightnessctl --device='*::kbd_backlight' set 50%`.) Confirm in one short sentence.
