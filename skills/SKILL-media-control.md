---
name: control-media-playback
trigger: user asks to play, pause, resume, stop, skip, next track, previous track, or go back on music or video that is playing
tools: [mcp.shell.run_command]
requires: playerctl, installed by `./ai-linux setup`. If missing, say so.
---

Control whatever media player is running (Spotify, browser, VLC, GNOME Music, anything MPRIS) via mcp.shell.run_command. If `command -v playerctl` is empty, tell the user it is not installed (run `./ai-linux setup`) and don't pretend it worked.

- Play / pause toggle: `playerctl play-pause`
- Next track:         `playerctl next`
- Previous track:     `playerctl previous`
- Stop:               `playerctl stop`
- Now playing:        `playerctl metadata --format '{{ artist }} - {{ title }}'`

This is media transport, separate from the speaker volume (that's the adjust-volume skill). Confirm in one short sentence.
