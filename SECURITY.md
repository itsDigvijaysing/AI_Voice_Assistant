# Security

AI Linux Assistant is a **local-first, single-user** desktop voice assistant. This document states its
threat model, what's guaranteed, and the safeguards.

## Threat model
- **Intended environment:** one trusted user on their own Linux machine (Ubuntu/GNOME/Wayland).
- **In scope:** the assistant should never silently escalate privileges, never expose a network service,
  never leak secrets, and should refuse obviously-catastrophic actions.
- **Out of scope:** a multi-tenant/shared host, or defending against the logged-in user attacking
  themselves. A determined adversary who already runs code as your user is not contained.

## What's guaranteed
- **No superuser at runtime.** The running assistant never calls `sudo`/`root`: every command runs as
  your user. (Verified: there is no `sudo`/`pkexec`/`setuid` in the runtime code path.)
- **The only privileged step is `./ai-linux setup`**, which does a one-time `apt` install of helper tools
  (`brightnessctl`, `playerctl`, `wl-clipboard`, `ydotool`), adds you to the `video` group (brightness),
  and installs a scoped **udev rule** so `ydotool` can use `/dev/uinput` via a per-session ACL,
  and deliberately **not** the `input` group, so no process gains system-wide keystroke read (which would
  defeat Wayland's input isolation). Package names are hardcoded (no injection). Screenshots go through
  GNOME's consent-based screen-capture **portal** (via `computer-use-linux`), never a silent grabber.
- **Fully reversible.** Setup records every system change to `~/.local/state/ai-linux/install-manifest.tsv`,
  and **`./ai-linux uninstall`** reverts exactly those deltas (`--dry-run` to preview, `--purge` for a deep
  clean). Nothing is installed that you can't cleanly remove.
- **No inbound network.** No listening socket is opened. Ollama is reached only on `127.0.0.1:11434`;
  the assistant is local-first by default; a cloud brain is only reached if you manually opt in by
  pointing `completion_url` at an OpenAI-compatible endpoint, and the Todoist tools reach
  `api.todoist.com` only if `TODOIST_API_TOKEN` is set. MCP tool servers use stdio.
- **No secrets in the repo.** API keys/tokens are read from environment variables only (`GLADOS_API_KEY` /
  `TODOIST_API_TOKEN` / …); configs ship `api_key: null`.
- **User-private state.** Runtime/IPC files (`$XDG_RUNTIME_DIR/ai-linux/`) and `data/` are created `0700`;
  TTS temp files use unpredictable names and are deleted after playback.

## How actions are controlled
1. **Action gate** (`core/tool_safety.py`): `mcp.shell.*`, `mcp.skills_actions.*` (the typed desktop
   tools) and `mcp.computer_use.*` are **denied by default** and run only when the session is *armed*
   (`GLADOS_ALLOW_ACTIONS=1`). Interactive `./ai-linux` arms them so the assistant can act;
   `./ai-linux --no-actions` runs it disarmed (chat/info only).
2. **Autonomy hard-floor:** the autonomous loop can **never** run gated actions, regardless of env/config.
3. **Destructive-command denylist** (`mcp/shell_exec.py`, the single execution chokepoint shared by
   `mcp.shell.run_command` AND every `mcp.skills_actions.*` tool): a conservative backstop that refuses
   clearly catastrophic commands (`rm -rf /`/`~`/`$HOME`/`/home`, including long-form `--recursive --force`,
   quoted, `~user`, glob (`<root>/*`, `<root>/.*`), and chained/`cd <root> &&` variants, `dd of=/dev/…`,
   `mkfs`/`wipefs`/`shred`/`truncate`/`tee` of a device, `find <root> … -delete`, redirect to a raw disk,
   fork bomb, `chmod/chown -R /`, `curl … | sh`)
   **regardless of how the command was produced** (model,
   skill, or learned skill). It matches the *literal* command text, so it **cannot** catch indirection:
   `X=/; rm -rf $X`, `eval "$cmd"`, `python -c 'shutil.rmtree(…)'`, `find / -exec rm -rf {} +`,
   `… | base64 -d | sh`, and similar. It is a safety net, **not a sandbox**; the **action gate** (1) is the
   real boundary.
4. **Prompt-injection mitigation:** the system prompt instructs the model to treat tool/file/web/screenshot
   text as untrusted *data*, never instructions, and to refuse data-destroying actions.

### Why there is no OS sandbox (evaluated 2026-07-05, don't re-litigate without a threat-model change)
Filesystem/namespace sandboxes (bubblewrap, firejail, Landlock, systemd `ProtectHome=`) were evaluated and
rejected for the shell executor: every desktop tool NEEDS the live session: `$XDG_RUNTIME_DIR` sockets
(Wayland, PipeWire, D-Bus), `gsettings`, `wpctl`, launching GUI apps, and a process holding an open
**session bus** can escape any such sandbox anyway (e.g. `org.freedesktop.systemd1 StartTransientUnit`
starts an unconfined unit). So a sandbox here would break the assistant's purpose while adding no real
boundary. What we DO use is kernel-enforced **resource containment**: `run_shell` wraps commands in a
`systemd-run --user --scope` with `TasksMax`/`MemoryMax` caps (graceful fallback to a plain subprocess
when unavailable), which contains fork bombs and runaway memory without restricting session access.
Re-evaluate only if the machine becomes multi-user or tools stop needing session access.

## Residual risk & recommendations
When armed, the LLM can run shell commands and control the desktop. That's the point of an assistant, but
it means a sufficiently clever **prompt injection** (spoken, or text in a file/screenshot it reads) could
attempt a harmful action. The denylist + gate + prompt framing reduce this; they don't eliminate it.
- Prefer **`qwen3:4b`** (or a cloud brain) over `qwen3:1.7b` for better injection resistance.
- Run **`./ai-linux --no-actions`** when you only need chat/answers.
- Learned skills (`skills/learned/`, from `save_skill`) are markdown only and never execute on their own;
  their commands still pass the gate + denylist when run.

## Reporting
This is a personal project; open an issue describing the concern (do not include secrets).
