from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from errorbook_mcp.config import Settings
from errorbook_mcp.server import _invoke, create_server


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
                    "answer_markdown": "B",
                },
                "idempotency_key": "stdio-create-0001",
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["ok"] is True
        assert result.structuredContent["problem"]["number"].startswith("EB-")
