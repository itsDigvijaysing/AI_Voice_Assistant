# Skills — procedure library

One markdown file per desktop procedure, short and imperative, with a small
front-matter block (`name`, `trigger`, `tools`).

**Role since the native-tools pivot (commit 305a97c):** the default runtime does NOT
retrieve these files per turn. Each desktop capability is a typed, named
function-calling tool in `glados.mcp.skills_actions_server` (gated, executed through the
shared denylisted `shell_exec.run_shell`); the commands in these SKILL files are the
curated source those tools were built from, and this library remains their reference
documentation.

The files are still consumed at runtime by:

- the **optional** `skills` MCP server (`glados.mcp.skills_server`: `list_skills` /
  `find_skill` — commented out in `configs/ai_linux_config.yaml`; re-enable to get
  keyword/hybrid retrieval over this library again),
- `/learn` (writes new drafts to `skills/learned/` via `skills_writer`), and
- `/tidy` (catalog + feedback review).

When a command here changes (or a capability is added/removed), update the matching
typed tool in `skills_actions_server.py` — the SKILL file alone no longer changes
runtime behavior.

## Adding a new capability (developer guide)

There are two ways a new capability gets added, and they're not equally reliable —
pick the first one unless you have a specific reason not to.

### 1. A new typed tool (the reliable, default path)

Every capability the model actually uses today is a small, named Python function in
an MCP server module. To add one:

1. Pick a home: a new `@mcp.tool()` function in an existing server (e.g.
   `skills_actions_server.py` for another desktop action), or a whole new server module
   under `src/glados/mcp/` (e.g. `todoist_server.py` — a real example: it wraps an
   external REST API, not a shell command, so it lives on its own).
2. Write the function with a short docstring — the docstring IS the tool description
   the model sees, so make it crisp and unambiguous (a vague description is the #1
   cause of a small model picking the wrong tool). Return a JSON string, `{ok: ...}` or
   `{error: ...}` on failure — never raise.
3. If the tool runs a local shell command, build it and pass it through
   `glados.mcp.shell_exec.run_shell` (never `subprocess` directly) so the destructive-
   command denylist and resource caps apply. If it's a network call to an external
   service (like Todoist), it doesn't touch `run_shell` at all — decide deliberately
   whether it needs gating (`core/tool_safety.py`'s `_DEFAULT_CONFIRM_PATTERNS`): local
   shell/GUI actions are gated by default, but a narrow, reversible external-API call
   doesn't have to be (see `voice_server.py` and `todoist_server.py` for the ungated
   reasoning).
4. Register the server in `configs/ai_linux_config.yaml`'s `mcp_servers` list. Keep the menu lean — a short, curated tool list is
   what makes a small local model pick the right tool reliably (verified); don't add a
   dozen tools for one feature.
5. Nudge `personality_preprompt` if the new capability is a category the model
   wouldn't otherwise think to call a tool for (see how the Todoist tools are called
   out by name in the system prompt).
6. Sanity-check the config loads and the module imports before calling it done:
   `conda run -n AI_Linux python -c "from glados.core.engine import GladosConfig; GladosConfig.from_yaml('configs/ai_linux_config.yaml')"`.

### 2. Self-learning at runtime (built, but off by default)

The assistant *can* learn a new skill on its own, at runtime, without a code change:
the `/learn` command (and the `skills_writer` MCP server's `save_skill()` tool) write
a new `SKILL-*.md` draft to `skills/learned/` (see `core/skills_index.py:write_skill`).
This is real and already built — but it's the pre-pivot design (inject a matching
skill's command text into context, let the model reason about it), and that's exactly
what turned out to be unreliable on this project's small local model (qwen3:4b): it
misfired on ambient words and couldn't consistently reason about an injected command
string. That's why `skills_writer` and the retrieval server (`mcp.skills`) are
commented out in the config by default. A learned draft here does NOT become a typed
tool automatically — promoting it to something the model calls reliably still means
doing path 1 above by hand. Treat this path as an experimental/optional feature to
re-enable and evaluate, not the primary way to extend the assistant.
