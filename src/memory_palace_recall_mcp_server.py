"""RQS-03 Recall MCP server bound to its startup cwd local database."""

from __future__ import annotations

import argparse
import os
import sqlite3
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from memory_palace.memory_recall_or_groups import recall_memories_or_groups
from memory_palace.project_paths import ProjectPathError, project_paths_for_cwd

SERVER_NAME = "memory-palace-recall"
TOOL_NAME = "memory_recall"
SERVER_INSTRUCTIONS = (
    "记忆宫殿只在确有历史信息需求时按需使用，不要每轮自动调用。"
    "用户要求回想、回忆、回顾，或问题明显依赖过往决定、约束、偏好和结果时，应考虑调用。"
    "每轮提交恰好四组同一语义方向的简短关键词组，可覆盖中英文、常见缩写或书写变体；不要把四个不同主题混在一起。"
    "每组内部按 AND 匹配，四组之间按 OR 合并并去重。"
    "每轮只调用一次；本轮结果相关就停止，不相关或为空才换一个语义方向。"
    "最多五轮，仍无相关结果就诚实说明没有召回到相关记忆。"
    "active 只表示未归档，不表示事实更真实。"
)
TOOL_DESCRIPTION = (
    "按四组同方向关键词从当前启动 cwd 的项目本地记忆库中只读召回去重后的最多 10 条记录。"
    "query_groups 必须恰好四组同一语义方向的中英文、缩写或书写变体；每组内部按 AND，组间按 OR。"
    "每轮只调用一次；本轮结果相关就停止，不相关或为空才换方向，最多五轮。"
    "active 只表示未归档，不表示事实更真实。"
)
QUERY_GROUPS_DESCRIPTION = (
    "恰好 4 个同一语义方向的简短关键词组；组内词按 AND、组间按 OR。"
    "可使用中英文、缩写或书写变体；不要放四个不同主题。"
)
SAFE_DATABASE_ERROR = "memory_recall_unavailable"


class RecallRecord(BaseModel):
    record_id: str
    title: str
    summary: str
    status: str
    as_of: str


class RecallResponse(BaseModel):
    query_groups: list[str]
    returned_count: int
    total_matches: int
    has_more: bool
    results: list[RecallRecord]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Memory Recall MCP STDIO server")
    return parser.parse_args(argv)


def _build_server(project_path: str) -> FastMCP:
    paths = project_paths_for_cwd(project_path)
    server = FastMCP(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS, log_level="ERROR")

    @server.tool(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def memory_recall(
        query_groups: Annotated[
            list[str], Field(description=QUERY_GROUPS_DESCRIPTION, min_length=4, max_length=4)
        ],
    ) -> RecallResponse:
        try:
            raw = recall_memories_or_groups(
                paths.database_path,
                project_id=paths.project_id,
                query_groups=query_groups,
                limit=10,
                status_scope="active",
            )
            return RecallResponse(
                query_groups=raw["query_groups"],
                returned_count=raw["returned_count"],
                total_matches=raw["total_matches"],
                has_more=raw["has_more"],
                results=[RecallRecord(**record) for record in raw["results"]],
            )
        except (sqlite3.Error, ProjectPathError):
            raise RuntimeError(SAFE_DATABASE_ERROR) from None

    return server


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)
    _build_server(os.getcwd()).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
