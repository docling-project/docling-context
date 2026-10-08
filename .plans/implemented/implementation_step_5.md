# Step 5: Agent harness installation, CLI, SDK, and MCP

## Goal and dependency

Make the context service easy to install and use from agent harnesses, locally first and with the same tools against a cloud service. Depends on stable storage, ingestion, retrieval, and memory operations from Steps 1–4.

## Installable product

- Publish a Python package with a stable CLI and `docling-context mcp` stdio entry point. Provide a remote HTTP MCP endpoint for deployments.
- Provide `docling-context integrate {codex,claude,hermes,pi}` with `--scope user|project` and `--mode local|remote`, plus `doctor` and `remove` operations. These are planned project commands, to be implemented in this step.
- Generate or update only this project's config entry. Preserve unrelated configuration, detect name conflicts, and make repeated installation idempotent. For project scope, create a reviewable project configuration file where the harness supports one; do not place credentials in it.
- Installation verifies the executable, MCP handshake, tool discovery, backend access, and one cited retrieval. `doctor` reports actionable failures; removal deletes only entries created by this installer. Test upgrade and rollback of generated configuration.
- Ship short harness-specific usage guidance so agents know when to call `search`, `outline`, and `show`, and how to retain URI/XPath citations. Keep memory capture opt-in and expose write tools explicitly.

## Public service contract

- Define a Python service layer with typed request/response objects for `list`, `tree`, `outline`, `show`, `search`, `ingest`, `job_status`, `session_append`, `memory_list`, `memory_review`, and `memory_recall`.
- `show` accepts a document URI, optional immutable revision, and XPath, then returns bounded DocLang XML or a structured representation with exact citation metadata. `outline` returns the TOC sidecar and node links. `search` returns matches and optional assembled context without silently changing tiers.
- Standardize errors: invalid URI/XPath, unauthorized scope, missing revision, stale citation, package failure, conversion failure, unsupported provider, and budget exceeded. Include stable machine-readable codes.
- Version the public request/response schema before adding network transport. Keep backend choice in configuration; callers use the same operations locally and with a deployed service.

## Surfaces

1. **Python SDK:** synchronous core operations, with asynchronous wrappers only where jobs or remote calls benefit. Return typed records rather than raw database rows. Keep package readers available for callers needing DocLang node operations.
2. **CLI:** commands such as `add`, `ls`, `tree`, `outline`, `show`, `find`, `jobs`, `sessions`, and `memories`. Provide human output and structured JSON output; bound tree depth and result count. `show` can emit DocLang XML, with no Markdown storage/export implied.
3. **MCP server:** expose read/search tools first. Define tool input schemas with URI, scope, XPath, tier, and limits. Add writes only with explicit authenticated principal and operation-specific authorization. Return citations and truncation markers in every content-bearing response.

## Harness targets

| Harness | Initial installation route |
| --- | --- |
| Codex | Register the MCP server through its CLI or configuration; evaluate a packaged plugin after direct installation works. |
| Claude Code | Register through `claude mcp` at user or project scope. |
| Hermes Agent | Manage a named `mcp_servers` entry in its configuration. |
| Pi | Register through built-in `pi mcp`; create a Pi package only if native Pi behavior adds value. |

Each adapter supports a local stdio server. Where the harness supports it, also support the remote HTTP endpoint with appropriate authentication. Harness adapters remain thin: every tool calls the same service contract. Maintain a compatibility matrix with tested harness versions, install scope, transport, verification, and removal behavior.

## Operational behavior

- Do not accept a caller-provided tenant or user ID as authorization by itself. Derive the principal from local configuration or authenticated transport, then check scope in the service layer.
- Enforce timeouts and budgets around conversion, model calls, retrieval, and XML output. Expose job IDs for slow ingestion rather than holding an interactive request indefinitely.
- Log operation ID, URI scope, latency, index generation, and result count; omit document content and credentials from default logs.
- Package optional agent framework adapters separately after the SDK is stable; harness installation is part of this step and calls the public service layer without adding independent storage semantics.

## Work sequence

1. Freeze the Python service schema and shared error types; package the local service, CLI, and stdio MCP entry point.
2. Implement MCP read tools with the same contract and bounds, then authorized write tools.
3. Implement harness configuration adapters, `doctor`, and removal. Test a fresh local install in each harness with a small DCLX fixture.
4. Add remote HTTP setup and authentication checks.
5. Publish installation guides and a compatibility matrix. Add framework adapters only where a concrete integration test exists.

## Acceptance checks

- For the same fixture and principal, SDK, CLI JSON, and MCP return equivalent URIs, revisions, XPaths, and content bounds.
- Bad scopes, oversized results, invalid XPath, and missing packages produce the defined errors on every surface.
- A local installation runs ingest, outline, search, and cited `show` without credentials, network, or a cloud account.
- A long conversion returns a job ID and can be polled after process restart.
- A new user can install the package, run one integration command, restart each supported harness, and retrieve a cited DocLang node.
- User and project installs coexist without overwriting unrelated settings. Reinstall, upgrade, and removal behave predictably for all four harnesses.
- All four harnesses expose equivalent read tools and citation fields. Local setup requires no cloud account; remote setup does not put secrets in project configuration.

## Exit artifact

An installable local product and verified integrations for Codex, Claude Code, Hermes, and Pi whose public operations can be served by the cloud backend without a contract change.

## Harness documentation to recheck at implementation time

- [Codex MCP setup](https://developers.openai.com/learn/docs-mcp)
- [Claude Code MCP setup](https://code.claude.com/docs/en/mcp)
- [Hermes Agent MCP setup](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/)
- [Pi MCP setup](https://pi.dev/docs/latest/mcp)
