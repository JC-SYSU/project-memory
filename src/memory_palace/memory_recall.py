"""Memory Recall Core v0.1 — 只读、纯词法检索（冻结方案 A）。

调用方给定 SQLite 数据库文件、Project ID 与查询文本，按 L01 冻结方案 A
打分排序，返回最多 limit 条记忆记录，并可同时取得截断前精确总数。

本模块只做只读检索：不写数据库、不联网、不读取环境变量、不做任何
语义或模型处理，也没有第二套检索路径。
"""

import sqlite3
from urllib.parse import quote

_VALID_STATUS_SCOPES = ("all", "active", "archived")


def _read_only_uri(database_path):
    """把调用方路径拼成只读 SQLite URI。

    quote 只编码路径段，避免路径中的 ? # % 被误当作 URI 参数；
    全程不带 write 标志与 immutable 标志。
    """
    return "file:" + quote(database_path, safe="/:") + "?mode=ro"


def _score_chunks(chunks, title, summary):
    """对一条记录的 title/summary（已 casefold）按方案 A 打分。

    返回 (ok, title_hits, occurrences)：任一 chunk 在标题与摘要中都
    找不到时 ok=False，该记录被排除；否则统计标题命中数与全部
    非重叠出现次数之和。
    """
    title_hits = 0
    occurrences = 0
    for chunk in chunks:
        in_title = chunk in title
        in_summary = chunk in summary
        if not (in_title or in_summary):
            return False, 0, 0
        if in_title:
            title_hits += 1
        occurrences += title.count(chunk) + summary.count(chunk)
    return True, title_hits, occurrences


def _recall_memories_impl(
    database_path, *, project_id, query, limit, status_scope="active"
):
    """执行唯一一套 Recall 筛选、打分、排序与只读读取逻辑。

    参数：
        database_path: Project 的 SQLite 主文件路径（只读打开，绝不创建）。
        project_id:    只查询完全相等的 Project ID。
        query:         查询文本，按空白拆成多个检索词。
        limit:         必须是大于 0 的整数（bool 不算），排序完成后截取。
        status_scope:  all / active / archived。

    返回：
        (list[dict], int)，列表每项键顺序固定为
        record_id, title, summary, status, as_of；整数是截断前总数。

    校验顺序固定：limit → status_scope → query；
    前三步完成前不会接触数据库。空或纯空白 query 直接返回 ([], 0)。
    数据库与 SQL 错误原样抛出 sqlite3.Error，不做包装或重试。
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError("recall_invalid_limit")
    if status_scope not in _VALID_STATUS_SCOPES:
        raise ValueError("recall_invalid_status_scope")
    chunks = query.strip().split()
    if not chunks:
        return [], 0
    chunks = [chunk.casefold() for chunk in chunks]

    conn = sqlite3.connect(_read_only_uri(database_path), uri=True)
    try:
        if status_scope == "all":
            params = (project_id, "active", "archived")
            sql = (
                "SELECT record_id, title, summary, status, as_of FROM records "
                "WHERE project_id = ? AND status IN (?, ?)"
            )
        else:
            params = (project_id, status_scope)
            sql = (
                "SELECT record_id, title, summary, status, as_of FROM records "
                "WHERE project_id = ? AND status = ?"
            )
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    matched = []
    for record_id, title, summary, status, as_of in rows:
        ok, title_hits, occurrences = _score_chunks(
            chunks, title.casefold(), summary.casefold()
        )
        if ok:
            matched.append(
                (record_id, title_hits, occurrences, as_of, status, title, summary)
            )

    # 多级排序：低优先级先排，再逐级升高（稳定排序保证高优先级覆盖低优先级）。
    # 完整排序之后才截取 limit。
    matched.sort(key=lambda row: row[0])                        # record_id 升序
    matched.sort(key=lambda row: row[3], reverse=True)          # as_of 原字符串降序
    matched.sort(key=lambda row: row[2], reverse=True)          # occurrences 降序
    matched.sort(key=lambda row: row[1], reverse=True)          # title_hits 降序
    if status_scope == "all":
        matched.sort(key=lambda row: 0 if row[4] == "active" else 1)  # active 整体优先

    results = [
        {
            "record_id": row[0],
            "title": row[5],
            "summary": row[6],
            "status": row[4],
            "as_of": row[3],
        }
        for row in matched[:limit]
    ]
    return results, len(matched)


def recall_memories(database_path, *, project_id, query, limit, status_scope="active"):
    """按冻结方案 A 从调用方指定的 SQLite 中召回记忆记录。

    该旧接口保留原有参数、校验、排序、字段和只读行为；截断前总数
    由同一套内部实现计算但不通过旧接口暴露。
    """
    results, _ = recall_memories_with_total(
        database_path,
        project_id=project_id,
        query=query,
        limit=limit,
        status_scope=status_scope,
    )
    return results


def recall_memories_with_total(
    database_path, *, project_id, query, limit, status_scope="active"
):
    """返回冻结五字段结果与截断前精确匹配总数。"""
    return _recall_memories_impl(
        database_path,
        project_id=project_id,
        query=query,
        limit=limit,
        status_scope=status_scope,
    )
