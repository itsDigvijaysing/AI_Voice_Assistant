---
name: network-status
trigger: user asks about the network, internet connection, wifi, am i online, what network am i on, my IP address, or whether the internet is working
tools: [mcp.shell.run_command]
requires: NetworkManager + coreutils (nmcli/ping/hostname), present on Ubuntu 26.04; user-level, no sudo
---

Check network state via mcp.shell.run_command (no sudo), then tell the user plainly (not raw output):

- Active connection name: `nmcli -t -f NAME connection show --active`
- Are we online?:         `ping -c1 -W1 8.8.8.8`   (returncode 0 = online)
- Local IP address:       `hostname -I`
- Wi-Fi signal/SSID:      `nmcli -t -f ACTIVE,SSID,SIGNAL dev wifi`

Summarize in one short sentence (e.g. "You're online on <network>." or "You appear to be offline.").
