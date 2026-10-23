"""Recall, health, and upstream-check MCP server.

``memory_recall`` stays bound to the startup cwd local database.
``memory_health`` scans every memory-palace database under the current user's
home directory.  ``memory_upstream_check`` previews the configured model
endpoint without network access and runs real probes only after an explicit
``confirm=true``.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from memory_palace.health_report import build_health_report, render_markdown
from memory_palace.memory_recall_or_groups import recall_memories_or_groups
from memory_palace.project_paths import ProjectPathError, project_paths_for_cwd
from memory_palace.upstream_probe import (
    load_config,
    missing_config_markdown,
    preview_markdown,
    render_report,
    run_probes,
)

SERVER_NAME = "memory-palace-recall"
TOOL_NAME = "memory_recall"
HEALTH_TOOL_NAME = "memory_health"
UPSTREAM_TOOL_NAME = "memory_upstream_check"
SERVER_INSTRUCTIONS = (
    "记忆宫殿存有本项目的会话历史：过往裁决、约束、偏好、实验结果和已完成工作。"
    "回答前先判断当前问题是否可能依赖这些历史——涉及项目名、文件路径、此前的决定或约定，"
    "用户提到之前、上次、当初，或你不确定当前上下文是否足够——只要可能依赖，就先调用 memory_recall 再回答；"
    "不确定时倾向调用一次，而不是凭上下文猜。"
    "与历史明确无关的全新问题不要调用。用户明确要求回想、回忆、回顾时必须先调用。"
    "每轮提交恰好四组同一语义方向的简短关键词组，可覆盖中英文、常见缩写或书写变体；不要把四个不同主题混在一起。"
    "每组内部按 AND 匹配，四组之间按 OR 合并并去重。"
    "每轮只调用一次；本轮结果相关就停止，不相关或为空才换一个语义方向。"
    "最多五轮，仍无相关结果就诚实说明没有召回到相关记忆；没有真实工具结果时不得声称已召回。"
    "active 只表示未归档，不表示事实更真实。"
    "当用户询问记忆健康、抽取队列、失败任务、待审数量或记忆是否正常时，调用 memory_health；"
    "它只读扫描本机所有项目的记忆库并返回固定格式报告，原样转述返回的 Markdown，不要自己重算。"
    "当用户询问上游模型、抽取端点或服务商连接是否正常时，先调用 memory_upstream_check 不带参数取得配置预览；"
    "向用户确认后，才用 confirm=true 运行真实探针。真实探针会调用上游模型并产生费用；用户没有明确要求时不要发起。"
)
TOOL_DESCRIPTION = (
    "按恰好四组同方向关键词从当前启动 cwd 的项目本地记忆库中只读召回去重后的最多 10 条记录。"
    "问题涉及过往裁决、约束、偏好、实验结果，或不确定当前上下文是否足够时先调用。"
    "query_groups 必须恰好四组同一语义方向的中英文、缩写或书写变体；每组内部按 AND，组间按 OR。"
    "每轮只调用一次；本轮结果相关就停止，不相关或为空才换方向，最多五轮。"
    "active 只表示未归档，不表示事实更真实。"
)
QUERY_GROUPS_DESCRIPTION = (
    "恰好 4 个同一语义方向的简短关键词组；组内词按 AND、组间按 OR。"
    "可使用中英文、缩写或书写变体；不要放四个不同主题。"
)
HEALTH_TOOL_DESCRIPTION = (
    "只读扫描本机所有项目的 .memory-palace 记忆库，返回固定格式的健康报告"
    "（抽取队列、审查队列、需要处理的库、失败明细、备注）。"
    "用户问健康检查、队列状态、有没有失败、多少待审、记忆是否正常时调用。"
    "无参数即可；不写库、不联网。返回 Markdown，原样转述。"
)
UPSTREAM_TOOL_DESCRIPTION = (
    "检查记忆抽取上游模型的连接与可用性。默认只返回配置预览，不发起请求；"
    "只有用户明确要求检查、并在看到预览后确认，才用 confirm=true 运行四项真实探针"
    "（会产生少量模型调用费用）。用户问上游连接、抽取端点、服务商是否正常时使用。"
    "返回 Markdown。"
)
UPSTREAM_CONFIRM_DESCRIPTION = (
    "false（默认）：只返回配置预览，不发请求。"
    "true：运行四项真实探针；会调用上游模型并产生费用，必须在用户明确确认后使用。"
)
SAFE_DATABASE_ERROR = "memory_recall_unavailable"
SAFE_HEALTH_ERROR = "memory_health_unavailable"


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

    @server.tool(
        name=HEALTH_TOOL_NAME,
        description=HEALTH_TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=False,
    )
    def memory_health() -> str:
        try:
            return render_markdown(build_health_report())
        except OSError:
            raise RuntimeError(SAFE_HEALTH_ERROR) from None

    @server.tool(
        name=UPSTREAM_TOOL_NAME,
        description=UPSTREAM_TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        structured_output=False,
    )
    def memory_upstream_check(
        confirm: Annotated[
            bool, Field(description=UPSTREAM_CONFIRM_DESCRIPTION)
        ] = False,
    ) -> str:
        config = load_config()
        if not confirm:
            return preview_markdown(config)
        if config.missing:
            return missing_config_markdown(config)
        return render_report(config, run_probes(config))

    return server


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)
    _build_server(os.getcwd()).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
