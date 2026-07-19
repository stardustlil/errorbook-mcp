from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from errorbook_mcp.config import Settings
from errorbook_mcp.schemas import MAX_REVIEW_NOTES_LENGTH, MAX_REVIEW_RESPONSE_LENGTH, ProblemDraft
from errorbook_mcp.server import _invoke, _parse_args, create_server


def test_mcp_surface_is_registered(settings: Settings) -> None:
    server = create_server(settings)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "create_problem",
        "get_problem",
        "search_problems",
        "update_problem",
        "record_review",
        "adjust_priority",
        "set_problem_status",
        "create_review_sheet",
        "get_export_status",
        "list_exports",
        "delete_export",
        "get_library_stats",
    }
    create = next(tool for tool in tools if tool.name == "create_problem")
    assert {"draft", "idempotency_key"}.issubset(set(create.inputSchema["required"]))
    assert create.inputSchema["properties"]["duplicate_policy"]["enum"] == [
        "return_existing",
        "create_anyway",
    ]
    status_tool = next(tool for tool in tools if tool.name == "set_problem_status")
    assert status_tool.inputSchema["properties"]["status"]["enum"] == [
        "active",
        "mastered",
        "archived",
    ]
    review_tool = next(tool for tool in tools if tool.name == "record_review")
    response_schema = review_tool.inputSchema["properties"]["response_markdown"]
    notes_schema = review_tool.inputSchema["properties"]["notes"]
    assert any(
        option.get("maxLength") == MAX_REVIEW_RESPONSE_LENGTH for option in response_schema["anyOf"]
    )
    assert any(
        option.get("maxLength") == MAX_REVIEW_NOTES_LENGTH for option in notes_schema["anyOf"]
    )

    prompts = asyncio.run(server.list_prompts())
    assert {prompt.name for prompt in prompts} == {"record_from_image"}
    templates = asyncio.run(server.list_resource_templates())
    assert any("errorbook://exports/" in str(template.uriTemplate) for template in templates)


def test_database_lock_is_reported_as_retryable() -> None:
    def locked() -> dict[str, object]:
        raise sqlite3.OperationalError("database is locked")

    result = _invoke(locked)
    assert result["error"]["code"] == "DATABASE_BUSY"
    assert result["error"]["retryable"] is True


def test_invoke_normalizes_validation_storage_and_internal_errors() -> None:
    def invalid_problem() -> dict[str, object]:
        ProblemDraft.model_validate({"kind": "solution", "subject": "", "stem_markdown": "x"})
        raise AssertionError("validation should fail")

    def storage_failure() -> dict[str, object]:
        raise sqlite3.OperationalError("disk I/O error")

    def unexpected_failure() -> dict[str, object]:
        raise RuntimeError("secret internal detail")

    validation = _invoke(invalid_problem)
    storage = _invoke(storage_failure)
    internal = _invoke(unexpected_failure)

    assert validation["error"]["code"] == "VALIDATION_ERROR"
    assert validation["error"]["details"]["issues"]
    assert storage["error"]["code"] == "STORAGE_ERROR"
    assert internal["error"]["code"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in internal["error"]["message"]


def test_cli_arguments_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "errorbook-mcp",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.2",
            "--port",
            "8123",
        ],
    )
    arguments = _parse_args()
    assert (arguments.transport, arguments.host, arguments.port) == (
        "streamable-http",
        "127.0.0.2",
        8123,
    )


@pytest.mark.asyncio
async def test_stdio_mcp_round_trip(tmp_path: Path) -> None:
    data_dir = tmp_path / "stdio-data"
    environment = dict(os.environ)
    environment.update(
        {
            "ERRORBOOK_DATA_DIR": str(data_dir),
            "ERRORBOOK_TIMEZONE": "Asia/Tokyo",
            "ERRORBOOK_LOG_LEVEL": "ERROR",
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "errorbook_mcp"],
        env=environment,
        cwd=str(Path.cwd()),
    )
    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert "create_problem" in {tool.name for tool in tools.tools}
        prompt = await session.get_prompt("record_from_image", {"subject_hint": "数学"})
        assert prompt.messages

        result = await session.call_tool(
            "create_problem",
            {
                "draft": {
                    "kind": "single_choice",
                    "subject": "数学",
                    "stem_markdown": "计算 $1+1$。",
                    "choices": [
                        {"label": "A", "content_markdown": "$1$"},
                        {"label": "B", "content_markdown": "$2$"},
                    ],
                },
                "idempotency_key": "stdio-create-0001",
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["ok"] is True
        assert result.structuredContent["problem"]["number"].startswith("EB-")


@pytest.mark.asyncio
async def test_streamable_http_mcp_round_trip(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    data_dir = tmp_path / "http-data"
    environment = dict(os.environ)
    environment.update(
        {
            "ERRORBOOK_DATA_DIR": str(data_dir),
            "ERRORBOOK_TIMEZONE": "Asia/Tokyo",
            "ERRORBOOK_LOG_LEVEL": "ERROR",
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "errorbook_mcp",
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        cwd=str(Path.cwd()),
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.returncode is not None:
                raise AssertionError(f"HTTP MCP server exited with {process.returncode}")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            await writer.wait_closed()
            break
        else:
            raise AssertionError("HTTP MCP server did not start")

        async with (
            httpx.AsyncClient(follow_redirects=True, trust_env=False) as http_client,
            streamable_http_client(
                f"http://127.0.0.1:{port}/mcp",
                http_client=http_client,
            ) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            result = await session.call_tool("get_library_stats", {})
            assert result.isError is False
            assert result.structuredContent is not None
            assert result.structuredContent["ok"] is True
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
