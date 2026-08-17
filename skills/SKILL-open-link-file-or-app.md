---
name: open-link-file-or-app
trigger: user asks to open / launch / go to / start a website, link, URL, file, folder, document, or application
tools: [mcp.shell.run_command]
requires: xdg-utils (xdg-open) + GTK (gtk-launch), present on Ubuntu 26.04 GNOME; actions are gated (armed by default)
---

Open things by running ONE command with `mcp.shell.run_command`:

- Open a website:        `xdg-open https://example.com`   (prepend `https://` if the user omits it)
- Web search:            `xdg-open "https://www.google.com/search?q=SEARCH+TERMS"`
- Open a file or folder:  `xdg-open "$HOME/Downloads"`   (quote paths; `$HOME`/`~` work)
- Launch an application:  `gtk-launch firefox`   (use the .desktop id; e.g. `org.gnome.Nautilus` = Files,
                          `org.gnome.Console` = Terminal). If unsure of the id, `xdg-open` a URL/file instead.

`xdg-open` uses the user's default app for that type. Confirm what you opened in one short sentence.
