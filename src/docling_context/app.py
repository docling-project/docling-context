"""Installable command for MCP serving and harness integration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .ingest_queue import IngestQueue
from .integration import IntegrationSpec, config_path, inspect, integrate
from .local import LocalContextStore
from .mcp_server import authenticated_http_app, create_mcp
from .models import Principal
from .service import ContextService


def _spec(args: argparse.Namespace) -> IntegrationSpec:
    return IntegrationSpec(
        args.harness,
        args.scope,
        args.mode,
        Path(args.project_root).resolve(),
        Path(args.home).expanduser().resolve(),
        Path(args.store).expanduser(),
        args.tenant,
        args.user,
        args.url,
        args.token_env,
        args.allow_writes,
    )


async def _probe(spec: IntegrationSpec) -> dict[str, object]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    if spec.mode == "local":
        command_args = [
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
            command_args.append("--allow-writes")
        transport = stdio_client(
            StdioServerParameters(command=sys.executable, args=command_args)
        )
    else:
        token = os.environ.get(spec.token_env)
        if not token:
            raise ValueError(f"set {spec.token_env} before remote doctor")
        assert spec.url is not None
        transport = streamablehttp_client(
            spec.url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    async with transport as streams, ClientSession(streams[0], streams[1]) as session:
        handshake = await session.initialize()
        available = await session.list_tools()
        names = {tool.name for tool in available.tools}
        needed = {"list_resources", "tree", "outline", "show", "search"}
        if not needed <= names:
            raise RuntimeError(f"missing MCP tools: {sorted(needed - names)}")
        if spec.allow_writes and "ingest" not in names:
            raise RuntimeError("MCP write tools were requested but not exposed")
        listing = await session.call_tool(
            "list_resources", {"uri": "docling://resources"}
        )
        if listing.isError:
            raise RuntimeError("backend listing failed")
        result: dict[str, object] = {
            "status": "ok",
            "protocol": handshake.protocolVersion,
            "tools": sorted(names),
            "backend": "reachable",
        }
        if spec.mode == "local":
            with LocalContextStore(spec.store.expanduser()) as store:
                row = store.db.execute(
                    """SELECT r.uri,r.xpath,r.revision_id,r.text FROM retrieval_units r
                       JOIN documents d ON d.tenant_id=r.tenant_id AND d.uri=r.uri
                       WHERE r.tenant_id=? AND r.uri LIKE 'docling://resources/%'
                       AND d.available=1 AND d.revision_id=r.revision_id
                       AND r.xpath NOT LIKE '@%' AND length(r.text)>3 LIMIT 1""",
                    (spec.tenant,),
                ).fetchone()
            if row is not None:
                cite = await session.call_tool(
                    "show",
                    {
                        "uri": row["uri"],
                        "xpath": row["xpath"],
                        "revision_id": row["revision_id"],
                        "max_chars": 256,
                    },
                )
                content = cite.structuredContent or {}
                if (
                    cite.isError
                    or "error" in content
                    or content.get("data", {}).get("revision_id") != row["revision_id"]
                    or content.get("data", {}).get("xpath") != row["xpath"]
                ):
                    raise RuntimeError("cited retrieval failed")
                result["citation"] = {
                    "uri": row["uri"],
                    "revision_id": row["revision_id"],
                    "xpath": row["xpath"],
                }
            else:
                result["citation"] = "no indexed document available"
        return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docling-context")
    commands = parser.add_subparsers(dest="command", required=True)
    mcp = commands.add_parser("mcp", help="Run the local or remote MCP server")
    mcp.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    mcp.add_argument(
        "--store",
        default=os.environ.get(
            "DOCLING_CONTEXT_STORE", "~/.local/share/docling-context"
        ),
        help="Local context store (default: ~/.local/share/docling-context)",
    )
    mcp.add_argument(
        "--tenant",
        default=os.environ.get("DOCLING_CONTEXT_TENANT", "default"),
        help="Principal tenant (default: default)",
    )
    mcp.add_argument(
        "--user",
        default=os.environ.get("DOCLING_CONTEXT_USER", "local"),
        help="Principal user (default: local)",
    )
    mcp.add_argument(
        "--host", default="127.0.0.1", help="HTTP bind address (default: loopback)"
    )
    mcp.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    mcp.add_argument(
        "--token-env",
        default="DOCLING_CONTEXT_TOKEN",
        help="Bearer token environment variable for HTTP",
    )
    mcp.add_argument(
        "--allow-writes",
        action="store_true",
        help="Expose ingestion, session, and memory write tools",
    )
    ingestion_worker = commands.add_parser(
        "ingest-worker", help="Process durable document ingestion jobs"
    )
    ingestion_worker.add_argument(
        "--store",
        default=os.environ.get(
            "DOCLING_CONTEXT_STORE", "~/.local/share/docling-context"
        ),
        help="Local context store (default: ~/.local/share/docling-context)",
    )
    ingestion_worker.add_argument(
        "--once", action="store_true", help="Process one queued job then exit"
    )
    for action in ("integrate", "doctor", "remove"):
        sub = commands.add_parser(
            action, help=f"{action.capitalize()} one harness MCP entry"
        )
        sub.add_argument("harness", choices=("codex", "claude", "hermes", "pi"))
        sub.add_argument(
            "--scope",
            choices=("user", "project"),
            default="user",
            help="Harness config scope (default: user)",
        )
        sub.add_argument(
            "--mode",
            choices=("local", "remote"),
            default="local",
            help="MCP transport mode (default: local stdio)",
        )
        sub.add_argument(
            "--project-root",
            default=".",
            help="Project configuration root (default: current directory)",
        )
        sub.add_argument("--home", default=str(Path.home()), help=argparse.SUPPRESS)
        sub.add_argument(
            "--store",
            default=os.environ.get(
                "DOCLING_CONTEXT_STORE", "~/.local/share/docling-context"
            ),
            help="Local context store (default: ~/.local/share/docling-context)",
        )
        sub.add_argument(
            "--tenant",
            default=os.environ.get("DOCLING_CONTEXT_TENANT", "default"),
            help="Local principal tenant (default: default)",
        )
        sub.add_argument(
            "--user",
            default=os.environ.get("DOCLING_CONTEXT_USER", "local"),
            help="Local principal user (default: local)",
        )
        sub.add_argument("--url", help="Remote HTTPS MCP endpoint including /mcp")
        sub.add_argument(
            "--token-env",
            default="DOCLING_CONTEXT_TOKEN",
            help="Remote bearer token environment variable",
        )
        sub.add_argument(
            "--allow-writes", action="store_true", help="Enable local MCP write tools"
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in {
        "mcp",
        "ingest-worker",
        "integrate",
        "doctor",
        "remove",
        "-h",
        "--help",
    }:
        from .cli import main as dc_main

        return dc_main(argv)
    args = _parser().parse_args(argv)
    try:
        if args.command == "mcp":
            store = LocalContextStore(Path(args.store).expanduser())
            service = ContextService(store, Principal(args.tenant, args.user))
            server = create_mcp(
                service,
                allow_writes=args.allow_writes,
                host=args.host,
                port=args.port,
            )
            if args.transport == "stdio":
                server.run(transport="stdio")
            else:
                token = os.environ.get(args.token_env)
                if not token:
                    raise ValueError(f"set {args.token_env} before serving HTTP")
                import uvicorn

                uvicorn.run(
                    authenticated_http_app(
                        server, token, service, allow_writes=args.allow_writes
                    ),
                    host=args.host,
                    port=args.port,
                )
            store.close()
            return 0
        if args.command == "ingest-worker":
            with LocalContextStore(Path(args.store).expanduser()) as store:
                queue = IngestQueue(store)
                if args.once:
                    job = queue.run_once()
                    print(json.dumps(job.to_dict() if job else {"status": "idle"}))
                else:
                    import time

                    try:
                        while True:
                            if queue.run_once() is None:
                                time.sleep(1)
                    except KeyboardInterrupt:
                        pass
            return 0
        spec = _spec(args)
        result: dict[str, object]
        if args.command == "integrate":
            config_path(spec)
            if spec.mode == "remote" and (
                spec.url is None or not spec.url.startswith("https://")
            ):
                raise ValueError("remote mode requires an HTTPS --url")
            if spec.mode == "remote" and spec.allow_writes:
                raise ValueError("configure write tools on the remote server")
            probe = asyncio.run(_probe(spec))
            result = dict(integrate(spec))
            result["verification"] = probe
        elif args.command == "remove":
            result = dict(integrate(spec, remove=True))
        else:
            result = dict(inspect(spec))
            if result["status"] == "configured":
                result.update(asyncio.run(_probe(spec)))
        print(json.dumps(result, sort_keys=True))
        return (
            0
            if result["status"]
            in {"ok", "installed", "updated", "current", "removed", "absent"}
            else 1
        )
    except Exception as exc:  # noqa: BLE001 - MCP clients raise exception groups
        cause: BaseException = exc
        while isinstance(cause, BaseExceptionGroup) and cause.exceptions:
            cause = cause.exceptions[0]
        print(f"docling-context: {cause}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
