---
name: adjust-volume-and-mute
trigger: user asks to change / raise / lower / set the volume, make it louder or quieter, or mute / unmute the speakers or sound
tools: [mcp.shell.run_command]
requires: PipeWire / WirePlumber (wpctl), present on Ubuntu 26.04; actions are gated (armed by default under ai-linux)
---

Control the speaker volume by running ONE command with `mcp.shell.run_command` (the audio sink is PipeWire, addressed as `@DEFAULT_AUDIO_SINK@`):

- Louder:            `wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%+`
- Quieter:           `wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-`
- Set to a level:    `wpctl set-volume @DEFAULT_AUDIO_SINK@ 50%`   (use the percent the user asked for)
- Mute / unmute:     `wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle`
- Force mute on/off: `wpctl set-mute @DEFAULT_AUDIO_SINK@ 1`   (use `0` to unmute)
- Check current:     `wpctl get-volume @DEFAULT_AUDIO_SINK@`

This is the speaker output volume, it is separate from the assistant's own microphone mute (the overlay's mic toggle). Confirm the result in one short sentence.
