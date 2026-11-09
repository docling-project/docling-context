"""Install a project-aware skill beside the owned MCP connection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .integration import IntegrationSpec, _atomic_write, inspect, integrate

AGENTS = ("codex", "claude", "hermes", "pi")
SKILL_PATHS = {
    "codex": Path(".agents/skills/dc/SKILL.md"),
    "claude": Path(".claude/skills/dc/SKILL.md"),
    "hermes": Path(".hermes/skills/dc/SKILL.md"),
    "pi": Path(".pi/agent/skills/dc/SKILL.md"),
}


def skill_path(spec: IntegrationSpec) -> Path:
    if spec.harness not in AGENTS:
        raise ValueError(f"unsupported agent: {spec.harness}")
    root = spec.project_root if spec.scope == "project" else spec.home
    return root / SKILL_PATHS[spec.harness]


def skill_text(agent: str) -> str:
    if agent not in AGENTS:
        raise ValueError(f"unsupported agent: {agent}")
    content = f"""---
name: dc
description: Work with a Docling Context project, search its resources, read exact citations, and record a project session.
---

# Docling Context

Use this skill when the user invokes dc or asks to work with a Docling Context project. This skill is installed for {agent}.

1. Determine the project. Use §DOCLING_CONTEXT_PROJECT§ when set. Otherwise call the §overview§ MCP tool and ask the user to choose if several projects fit. Never silently search every project.
2. Determine the session. If §DOCLING_CONTEXT_SESSION§ is set, use that full URI and do not start another session. Otherwise call §project_session_start§ with the project ID, a fresh session ID, and §agent="{agent}"§. Keep the returned URI for later events.
3. Call §project_show§ and §project_resources§. Every project links §docling://resources/skills/doclang-authoring§. Before writing DCLG XML or a DCLX package, read it with §show(uri="docling://resources/skills/doclang-authoring", xpath="/doclang[1]/text[1]")§ and follow its validation instructions. Use §search(project_id=...)§ to find relevant nodes, then §show§ with the returned URI, revision ID, and XPath for exact citations. Use §outline§ to navigate a long document.
4. Record useful decisions, conclusions, and corrections with §session_append§ using the full session URI, a unique key, and a suitable event kind.
5. When you started the session yourself, call §project_session_close§ at the end. If §DOCLING_CONTEXT_SESSION§ was supplied by the launcher, leave closure to the launcher.

For terminal work, use the canonical CLI groups: §dc library add/list/search/show§ for source documents, §dc project resources§ for membership, §dc memory§ for reviewed conclusions, §dc concept§ for schema definitions, and §dc knowledge§ for cited facts defined by a concept. §dc session start --project ID --agent {agent}§ launches an interactive session; do not run it from an already active agent session.

If Docling Context MCP tools are missing, tell the user to run §dc agent install {agent}§ and restart this agent. Do not invent citations or claim that unrecorded turns were captured.
"""
    return content.replace("§", chr(96))


def _marker(path: Path) -> Path:
    return path.with_name(".docling-context-managed.json")


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _owned(path: Path) -> bool:
    marker = _marker(path)
    if not path.exists() or not marker.exists():
        return False
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return info.get("sha256") == _digest(path.read_text(encoding="utf-8"))


def _check_skill(path: Path) -> None:
    if path.exists() and not _owned(path):
        raise ValueError(f"{path}: existing dc skill is not managed by this installer")


def _write_skill(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    marker = _marker(path)
    previous_marker = marker.read_text(encoding="utf-8") if marker.exists() else None
    try:
        _atomic_write(path, content)
        _atomic_write(
            marker, json.dumps({"sha256": _digest(content)}, sort_keys=True) + "\n"
        )
    except OSError:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, previous)
        if previous_marker is None:
            marker.unlink(missing_ok=True)
        else:
            _atomic_write(marker, previous_marker)
        raise


def install_agent(spec: IntegrationSpec) -> dict[str, str]:
    path = skill_path(spec)
    content = skill_text(spec.harness)
    _check_skill(path)
    mcp = integrate(spec)
    try:
        _write_skill(path, content)
    except OSError:
        if mcp["status"] == "installed":
            integrate(spec, remove=True)
        raise
    return {"status": mcp["status"], "mcp": mcp["path"], "skill": str(path)}


def inspect_agent(spec: IntegrationSpec) -> dict[str, str]:
    path = skill_path(spec)
    mcp = inspect(spec)
    return {
        "status": "configured"
        if mcp["status"] == "configured" and _owned(path)
        else "incomplete",
        "mcp": mcp["path"],
        "skill": str(path),
        "skill_status": "managed"
        if _owned(path)
        else ("unmanaged" if path.exists() else "absent"),
    }


def remove_agent(spec: IntegrationSpec) -> dict[str, str]:
    path = skill_path(spec)
    if path.exists() and not _owned(path):
        raise ValueError(f"{path}: existing dc skill is not managed by this installer")
    mcp = integrate(spec, remove=True)
    if path.exists():
        path.unlink()
    _marker(path).unlink(missing_ok=True)
    return {"status": mcp["status"], "mcp": mcp["path"], "skill": str(path)}
