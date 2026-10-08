"""Install only the managed MCP entry in each supported harness configuration."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
from ruamel.yaml import YAML

NAME = "docling-context"


@dataclass(frozen=True, slots=True)
class IntegrationSpec:
    harness: str
    scope: str = "user"
    mode: str = "local"
    project_root: Path = field(default_factory=Path.cwd)
    home: Path = field(default_factory=Path.home)
    store: Path = Path("~/.local/share/docling-context")
    tenant: str = "default"
    user: str = "local"
    url: str | None = None
    token_env: str = "DOCLING_CONTEXT_TOKEN"
    allow_writes: bool = False


def config_path(spec: IntegrationSpec) -> Path:
    root = spec.project_root if spec.scope == "project" else spec.home
    if spec.harness == "codex":
        return root / ".codex" / "config.toml"
    if spec.harness == "claude":
        return root / (".mcp.json" if spec.scope == "project" else ".claude.json")
    if spec.harness == "pi":
        return (
            root / ".pi" / ("mcp.json" if spec.scope == "project" else "agent/mcp.json")
        )
    if spec.harness == "hermes" and spec.scope == "user":
        return root / ".hermes" / "config.yaml"
    raise ValueError("unsupported harness or scope; Hermes supports user scope only")


def _entry(spec: IntegrationSpec) -> dict[str, Any]:
    if spec.mode == "local":
        args = [
            "-m",
            "docling_context.app",
            "mcp",
            "--store",
            str(spec.store.expanduser().resolve()),
            "--tenant",
            spec.tenant,
            "--user",
            spec.user,
        ]
        if spec.allow_writes:
            args.append("--allow-writes")
        return {"command": sys.executable, "args": args}
    if spec.mode != "remote" or not spec.url or not spec.url.startswith("https://"):
        raise ValueError("remote mode requires an HTTPS --url")
    if spec.allow_writes:
        raise ValueError(
            "configure write tools on the remote server, not the client entry"
        )
    if not spec.token_env.isidentifier():
        raise ValueError("token environment variable name is invalid")
    if spec.harness == "codex":
        return {"url": spec.url, "bearer_token_env_var": spec.token_env}
    entry = {
        "url": spec.url,
        "headers": {"Authorization": f"Bearer ${{{spec.token_env}}}"},
    }
    if spec.harness == "claude":
        entry["type"] = "http"
    return entry


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _load(path: Path, harness: str) -> Any:
    if not path.exists():
        return tomlkit.document() if harness == "codex" else {}
    content = path.read_text(encoding="utf-8")
    if harness == "codex":
        return tomlkit.parse(content)
    if harness == "hermes":
        return YAML(typ="rt").load(content) or {}
    return json.loads(content)


def _render(value: Any, harness: str) -> str:
    if harness == "codex":
        return tomlkit.dumps(value)
    if harness == "hermes":
        stream = io.StringIO()
        YAML(typ="rt").dump(value, stream)
        return stream.getvalue()
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _mapping(config: Any, harness: str) -> Any:
    key = "mcp_servers" if harness in {"codex", "hermes"} else "mcpServers"
    if key not in config:
        config[key] = tomlkit.table() if harness == "codex" else {}
    mapping = config[key]
    if not hasattr(mapping, "get"):
        raise ValueError(f"{key} must be a mapping")
    return mapping


def _plain(value: Any) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.docling-context-managed.json")


def integrate(spec: IntegrationSpec, *, remove: bool = False) -> dict[str, str]:
    path = config_path(spec)
    marker = _marker(path)
    config = _load(path, spec.harness)
    mapping = _mapping(config, spec.harness)
    current = _plain(mapping[NAME]) if NAME in mapping else None
    owned = json.loads(marker.read_text()) if marker.exists() else None
    if current is not None and (
        owned is None or _digest(current) != owned.get("digest")
    ):
        raise ValueError(
            f"{path}: existing {NAME} entry is not managed by this installer"
        )
    if remove:
        if current is None:
            return {"status": "absent", "path": str(path)}
        del mapping[NAME]
        _atomic_write(path, _render(config, spec.harness))
        marker.unlink(missing_ok=True)
        return {"status": "removed", "path": str(path)}
    entry = _entry(spec)
    if current == entry:
        return {"status": "current", "path": str(path)}
    mapping[NAME] = entry
    previous = path.read_text() if path.exists() else None
    try:
        _atomic_write(path, _render(config, spec.harness))
        _atomic_write(marker, json.dumps({"digest": _digest(entry)}, indent=2) + "\n")
    except OSError:
        if previous is not None:
            _atomic_write(path, previous)
        else:
            path.unlink(missing_ok=True)
        raise
    return {
        "status": "updated" if current is not None else "installed",
        "path": str(path),
    }


def inspect(spec: IntegrationSpec) -> dict[str, str]:
    path = config_path(spec)
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    config = _load(path, spec.harness)
    mapping = _mapping(config, spec.harness)
    if NAME not in mapping:
        return {"status": "missing", "path": str(path)}
    marker = _marker(path)
    if not marker.exists() or _digest(_plain(mapping[NAME])) != json.loads(
        marker.read_text()
    ).get("digest"):
        return {"status": "conflict", "path": str(path)}
    return {"status": "configured", "path": str(path)}
