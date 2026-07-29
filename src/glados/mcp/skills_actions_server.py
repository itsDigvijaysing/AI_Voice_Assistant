"""Desktop skills exposed as native function-calling tools (AI_Linux).

Instead of retrieving a SKILL-*.md and injecting a command string into the user turn (which a small
model can't reliably reason about and which misfired on ambient words), each desktop capability is a
NAMED, typed tool the model selects natively. The tool list IS the model's capability list, so it is
genuinely aware of what it can do. Each tool builds the exact command from the matching SKILL-*.md and
runs it through the shared gated executor (glados.mcp.shell_exec.run_shell) — so the destructive-command
denylist and the action gate (mcp.skills_actions.* is a gated family) apply exactly as for mcp.shell.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import urllib.parse

from loguru import logger
from mcp.server.fastmcp import FastMCP

from glados.mcp.shell_exec import run_shell

logger.remove()
logging.getLogger().setLevel(logging.CRITICAL)

mcp = FastMCP("skills_actions")

_SINK = "@DEFAULT_AUDIO_SINK@"
_SETTINGS_PANELS = {"sound", "display", "wifi", "network", "bluetooth", "power", "notifications", "privacy"}
_KNOWN_FOLDERS = {
    "home": "$HOME", "downloads": "$HOME/Downloads", "documents": "$HOME/Documents",
    "pictures": "$HOME/Pictures", "desktop": "$HOME/Desktop", "music": "$HOME/Music", "videos": "$HOME/Videos",
}


def _run(command: str) -> str:
    """Execute a fixed/curated command through the shared gated executor; return its JSON result."""
    return json.dumps(run_shell(command))


def _launch(command: str) -> str:
    """Launch a foreground GUI command detached so it outlives the tool call.

    Without this, run_shell's 20s timeout would block the turn and then SIGKILL the app the user
    just opened (sh -c execs a single command directly). setsid -f forks it into its own session,
    and the /dev/null redirect keeps the child from holding run_shell's capture pipes open.
    resource_caps=False: the app must NOT live out its life inside run_shell's capped scope.

    setsid -f forking successfully always returns 0, even if the app then fails to start — so
    once detached there is no rc left to trust. The one failure mode we CAN check synchronously,
    with no race, is "the binary isn't on PATH at all" — do that before forking so a typo'd/
    uninstalled app gets an honest error instead of a false "opened" (matches _launch_app's
    existing which() check; this covers the other callers, e.g. gnome-control-center).
    """
    try:
        binary = shlex.split(command)[0]
    except (ValueError, IndexError):
        binary = ""
    if binary and not shutil.which(binary):
        return json.dumps({"error": f"'{binary}' is not installed or not on PATH"})
    return json.dumps(run_shell(f"setsid -f {command} >/dev/null 2>&1", resource_caps=False))


# Friendly app name -> executable / .desktop id (binary preferred when it's on PATH; robust vs gtk-launch).
_APP_ALIASES = {
    "brave": "brave-browser", "brave browser": "brave-browser", "brave-browser": "brave-browser",
    "chrome": "google-chrome", "google chrome": "google-chrome", "chromium": "chromium",
    "firefox": "firefox",
    "vscode": "code", "vs code": "code", "code": "code", "visual studio code": "code",
    "files": "nautilus", "file manager": "nautilus", "nautilus": "nautilus",
    "terminal": "ptyxis", "console": "ptyxis",
    "text editor": "gnome-text-editor", "editor": "gnome-text-editor",
    "calculator": "gnome-calculator",
}
_SEARCH_SITES = {
    "web": "https://www.google.com/search?q=", "google": "https://www.google.com/search?q=",
    "youtube": "https://www.youtube.com/results?search_query=",
    "maps": "https://www.google.com/maps/search/",
    "github": "https://github.com/search?q=",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search=",
}


def _is_url(s: str) -> bool:
    low = s.lower()
    return "://" in s or low.startswith("www.") or bool(
        re.match(r"^[\w.-]+\.(com|org|net|io|dev|gov|edu|co|ai|app|tv)(/|$)", low)
    )


def _launch_app(name: str) -> str:
    """Launch a GUI app by friendly name: alias -> run the binary if on PATH -> gtk-launch -> xdg-open."""
    key = re.sub(r"\s+", " ", name.strip().lower())
    target = _APP_ALIASES.get(key, key.replace(" ", "-"))
    if shutil.which(target):
        return _launch(shlex.quote(target))  # launch the binary directly (works on Wayland; no gtk-launch quirks)
    return _run(f"gtk-launch {shlex.quote(target)} || xdg-open {shlex.quote(name)}")  # .desktop id, then guess


@mcp.tool()
def set_screen_brightness(percent: int) -> str:
    """Set the display (screen) brightness to a percentage from 0 to 100."""
    p = max(1, min(100, int(percent)))  # avoid 0% (screen off); clamp to sane range
    return _run(f"brightnessctl set {p}%")


@mcp.tool()
def set_volume(action: str = "", percent: int = -1) -> str:
    """Change the speaker volume. Use action 'up'/'down'/'mute'/'unmute', or set an exact level with percent (0-100)."""
    if 0 <= int(percent) <= 100:
        return _run(f"wpctl set-volume {_SINK} {int(percent)}%")
    a = (action or "").strip().lower()
    cmd = {
        "up": f"wpctl set-volume {_SINK} 5%+",
        "down": f"wpctl set-volume {_SINK} 5%-",
        "mute": f"wpctl set-mute {_SINK} 1",
        "unmute": f"wpctl set-mute {_SINK} 0",
    }.get(a)
    if not cmd:
        return json.dumps({"error": "set_volume needs action up|down|mute|unmute or a percent 0-100"})
    return _run(cmd)


@mcp.tool()
def lock_screen() -> str:
    """Lock the screen. Locking ONLY — this never suspends, sleeps, logs out, or turns off the computer."""
    return _run("loginctl lock-session")


@mcp.tool()
def take_screenshot(window: str = "") -> str:
    """Take a screenshot and save it (whole screen, or a specific window by title)."""
    w = (window or "").strip()
    if w:
        return _run(f"ai-linux-screenshot --window {shlex.quote(w)}")
    return _run("ai-linux-screenshot --full")


@mcp.tool()
def open_app_or_link(target: str) -> str:
    """Open/launch an application (e.g. 'brave', 'vs code', 'files'), a file/folder path, or a website URL."""
    t = (target or "").strip()
    if not t:
        return json.dumps({"error": "open_app_or_link needs a target (app name, path, or URL)"})
    if _is_url(t):
        url = t if "://" in t else "https://" + t
        return _run(f"xdg-open {shlex.quote(url)}")
    if t.startswith(("/", "~", "$HOME")):
        # shlex.quote single-quotes the arg, which suppresses ~ and $HOME expansion — so resolve them
        # to an absolute path FIRST (xdg-open does no expansion itself), then quote the resolved path.
        p = os.path.expanduser(os.path.expandvars(t))
        return _run(f"xdg-open {shlex.quote(p)}")
    return _launch_app(t)  # an application name -> resolve + launch


@mcp.tool()
def search_web(query: str, site: str = "web") -> str:
    """Search the web in the browser. site = 'web'/'google', 'youtube', 'maps', 'github', or 'wikipedia'."""
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "search_web needs a query"})
    base = _SEARCH_SITES.get((site or "web").strip().lower(), _SEARCH_SITES["web"])
    return _run(f"xdg-open {shlex.quote(base + urllib.parse.quote(q))}")


@mcp.tool()
def control_media(action: str) -> str:
    """Control the currently-playing media. action = 'play'/'pause'/'next'/'previous'/'stop'."""
    a = (action or "").strip().lower()
    cmd = {
        "play": "playerctl play-pause", "pause": "playerctl play-pause", "resume": "playerctl play-pause",
        "next": "playerctl next", "previous": "playerctl previous", "back": "playerctl previous",
        "stop": "playerctl stop",
    }.get(a)
    if not cmd:
        return json.dumps({"error": "control_media needs action play|pause|next|previous|stop"})
    return _run(cmd)


@mcp.tool()
def toggle_night_light(on: bool) -> str:
    """Turn the Night Light (blue-light filter / warm screen) on or off."""
    val = "true" if on else "false"
    return _run(f"gsettings set org.gnome.settings-daemon.plugins.color night-light-enabled {val}")


@mcp.tool()
def set_do_not_disturb(on: bool) -> str:
    """Turn Do Not Disturb (silence notification banners) on or off."""
    # DND on == hide banners (show-banners false)
    val = "false" if on else "true"
    return _run(f"gsettings set org.gnome.desktop.notifications show-banners {val}")


@mcp.tool()
def open_settings(panel: str = "") -> str:
    """Open GNOME Settings, optionally a panel: sound, display, wifi, network, bluetooth, power, notifications, privacy."""
    p = (panel or "").strip().lower()
    if p in _SETTINGS_PANELS:
        return _launch(f"gnome-control-center {p}")
    return _launch("gnome-control-center")


@mcp.tool()
def open_terminal() -> str:
    """Open a terminal window."""
    return _run("gtk-launch org.gnome.Ptyxis")


@mcp.tool()
def open_file_manager(folder: str = "") -> str:
    """Open the file manager, optionally at a folder: home, downloads, documents, pictures, desktop, music, videos."""
    key = (folder or "").strip().lower().strip("/").split("/")[0]
    path = _KNOWN_FOLDERS.get(key, "$HOME")
    return _run(f'xdg-open "{path}"')


@mcp.tool()
def clipboard(action: str, text: str = "") -> str:
    """Use the clipboard. action = 'copy' (with text) or 'read'/'paste' (returns the current clipboard)."""
    a = (action or "").strip().lower()
    if a == "copy":
        if not text:
            return json.dumps({"error": "clipboard copy needs text"})
        return _run(f"wl-copy {shlex.quote(text)}")
    if a in ("read", "paste", "get"):
        return _run("wl-paste")
    return json.dumps({"error": "clipboard needs action copy|read"})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
