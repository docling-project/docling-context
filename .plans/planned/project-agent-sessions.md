# Project agent sessions and agent command installation

Status: implemented in the working tree. The plan remains here as the requested implementation record.

## Goal

Support two entry points into one project-scoped session model:

1. `dc session start --project materials --agent codex` starts a durable project session, prepares the selected agent's Docling Context MCP connection and `dc` skill, runs the agent in the current terminal, and returns to the caller when it exits.
2. `dc agent install codex` installs the same MCP connection and reusable `dc` skill for an agent started independently. The skill guides project selection, session recording, scoped search, citations, and closure.

Support Codex, Claude Code, Hermes, and Pi. Use the existing local SQLite store and DCLX session packages. Keep the original caller's working directory and terminal.

## Current state

- The CLI has separate user-scoped `session` commands and `project session-*` commands. Project sessions already have DCLX URIs and a catalog link, but no agent identity.
- `docling-context integrate` installs an MCP entry only. The MCP tool set already includes project-aware search, project session start/close, and session append.
- Project refresh jobs generate deterministic abstract/overview inventory documents. They are unrelated to launching agents or session recording.
- Graphify installs agent skills under per-agent user/project paths. Codex's current official skill syntax is `$dc`; Claude, Hermes, and Pi expose skills through their own command menus.

## Proposed implementation

### Session model and CLI

- Store `agent` in `dc_project_sessions`. Keep a local additive schema update for existing SQLite stores.
- Make `dc session start` require `--project` and `--agent`. Generate or accept `--id`. Add `--no-launch` for an agent already running (the skill path). Return the session URI, project, and agent.
- On normal launch, verify the agent executable and install its MCP and skill integration, then start the DCLX session. Pass the full session URI, project ID, agent, store, tenant, and user to the child as environment variables and a short initial prompt. MCP searches default to that project. Wait for the agent process; on exit, close the session, which queues memory compilation. Preserve exit code and terminal control. Do not automatically run a memory worker.
- Show project sessions, including agent, with `dc session list --project PROJECT_ID`; project sessions also appear in `dc project session-list`.
- Keep explicit project session commands and the internal user session API for compatibility; the main human start path is project first.

### Agent installation

- Add `dc agent install|status|remove AGENT --scope user|project`. Installation reuses the owned MCP entry in `integration.py` and adds a small `dc` skill in the agent's skill directory. Installation is idempotent; refuse to overwrite unmanaged files and remove only owned files.
- Launcher installs user-scoped integration so it works from any directory. Explicit project scope writes into the current working directory for Codex, Claude, and Pi. Hermes uses user scope for its MCP integration. Skill paths follow the installed agents and the Graphify reference.
- The skill uses the Docling Context MCP tools for project-scoped retrieval and DCLX session events. When independently invoked, it starts a session for an explicit project, or asks for a project if several are available. It does not launch another agent.
- The launcher reuses the installer. No shell interpolation for agent arguments; use an argument list and inherited terminal streams.

### Documentation and verification

- Update `docs/index.md`, `docs/agents.md`, `docs/sessions.md`, and `docs/cli.md` with the two workflows, project scoping, agent-specific invocation names, and job distinctions.
- Unit tests cover project/agent requirement, DCLX and catalog association, subprocess argument/environment construction, exit and failure handling, and idempotent installer ownership in temporary homes/project roots.
- Run CLI help and discovery checks against installed `codex`, `claude`, `hermes`, and `pi` binaries. Do not start live model conversations during tests.

## Acceptance

- Running `dc session start --project materials --agent codex` enters Codex and returns to the original shell when Codex exits.
- The session is visible under the project with its agent and closed state.
- `dc agent install` makes the agent's Docling Context tools and skill available without starting through `dc`.
- Existing unrelated agent configuration and skills are preserved.
- Docs identify that Codex invokes the skill as `$dc`, while other harnesses may display `/dc`.

## Verification

- Full test suite: 111 passed.
- Ruff and mypy passed for the source tree.
- The installed Codex, Claude Code, Hermes, and Pi executables answered their version and help commands. Interactive model sessions were exercised with a subprocess test double rather than live prompts.
