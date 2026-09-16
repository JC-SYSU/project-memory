"""Memory Recall OR Groups — RQS-03 四组 OR 合并分轮候选实现。

接收恰好 4 个 query_groups，每组内部按方案 A（AND），四组之间按 OR，
返回去重并集，最多 10 条。单次调用只读打开数据库一次、执行一次 SELECT。
"""

import sqlite3
from urllib.parse import quote


def _read_only_uri(database_path):
    """只读 SQLite URI。"""
    return "file:" + quote(database_path, safe="/:") + "?mode=ro"


def _score_chunks(chunks, title, summary):
    """对一条记录的 title/summary（已 casefold）按方案 A 打分。
    
    返回 (ok, title_hits, occurrences)。
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


def recall_memories_or_groups(
    database_path, *, project_id, query_groups, limit=10, status_scope="active"
):
    """按四组 OR 合并从 SQLite 召回记忆记录。
    
    参数：
        database_path: SQLite 主文件路径（只读）。
        project_id:    只查询完全相等的 Project ID。
        query_groups:  必须恰好 4 个字符串，每个 strip() 后非空，casefold 后互不相同。
        limit:         全局截断上限，默认 10。
        status_scope:  all / active / archived。
    
    返回：
        {
          "query_groups": [...],
          "returned_count": int,
          "total_matches": int,
          "has_more": bool,
          "results": [...]
        }
    
    校验：
        - query_groups 必须是 list，长度恰好 4。
        - 每项必须是 str，strip() 后非空。
        - casefold 后四项互不相同。
        - limit 必须是 int 且 > 0。
        - status_scope 必须在 all/active/archived 中。
    
    错误输入抛 ValueError，数据库错误原样抛 sqlite3.Error。
    """
    # 输入校验
    if not isinstance(query_groups, list) or len(query_groups) != 4:
        raise ValueError("recall_or_groups_require_exactly_4_groups")
    
    if type(limit) is not int or limit <= 0:
        raise ValueError("recall_invalid_limit")
    
    if status_scope not in ("all", "active", "archived"):
        raise ValueError("recall_invalid_status_scope")
    
    # 校验每个 query_group
    stripped_groups = []
    seen_casefold = set()
    for item in query_groups:
        if not isinstance(item, str):
            raise ValueError("recall_or_groups_all_must_be_strings")
        s = item.strip()
        if not s:
            raise ValueError("recall_or_groups_empty_after_strip")
        cf = s.casefold()
        if cf in seen_casefold:
            raise ValueError("recall_or_groups_duplicate_after_casefold")
        seen_casefold.add(cf)
        stripped_groups.append(s)
    
    # 准备每组的 chunks
    groups_chunks = []
    for s in stripped_groups:
        chunks = s.split()
        if not chunks:
            # strip 后非空但 split 为空不可能，保险起见
            raise ValueError("recall_or_groups_empty_after_split")
        groups_chunks.append([chunk.casefold() for chunk in chunks])
    
    # 只读打开数据库一次，执行一次 SELECT
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
    
    # 对每条 Record 计算四组的匹配
    record_scores = {}  # record_id -> (best_title_hits, best_occurrences, as_of, status, title, summary, matched_group_indices)
    
    for record_id, title, summary, status, as_of in rows:
        title_cf = title.casefold()
        summary_cf = summary.casefold()
        
        best_title_hits = -1
        best_occurrences = -1
        matched_group_indices = []
        
        for group_idx, chunks in enumerate(groups_chunks):
            ok, title_hits, occurrences = _score_chunks(chunks, title_cf, summary_cf)
            if ok:
                matched_group_indices.append(group_idx)
                if title_hits > best_title_hits or (title_hits == best_title_hits and occurrences > best_occurrences):
                    best_title_hits = title_hits
                    best_occurrences = occurrences
        
        if matched_group_indices:
            record_scores[record_id] = (
                best_title_hits,
                best_occurrences,
                as_of,
                status,
                title,
                summary,
                matched_group_indices,
            )
    
    # 全局排序：active → title_hits 降序 → occurrences 降序 → as_of 降序 → record_id 升序
    class ReverseStr:
        """Wrapper for reverse string comparison."""
        def __init__(self, s):
            self.s = s
        def __lt__(self, other):
            return self.s > other.s
        def __le__(self, other):
            return self.s >= other.s
        def __gt__(self, other):
            return self.s < other.s
        def __ge__(self, other):
            return self.s <= other.s
        def __eq__(self, other):
            return self.s == other.s

    sorted_records = sorted(
        record_scores.items(),
        key=lambda item: (
            0 if item[1][3] == "active" else 1,  # active 优先
            -item[1][0],                           # title_hits 降序
            -item[1][1],                           # occurrences 降序
            ReverseStr(item[1][2]),                # as_of 降序
            item[0],                               # record_id 升序
        )
    )
    
    total_matches = len(sorted_records)
    has_more = total_matches > limit
    
    # 截取前 limit 条
    returned = sorted_records[:limit]
    
    # 构造冻结五字段 results。
    results = []
    for record_id, (_, _, as_of, status, title, summary, _) in returned:
        results.append({
            "record_id": record_id,
            "title": title,
            "summary": summary,
            "status": status,
            "as_of": as_of,
        })
    
    return {
        "query_groups": stripped_groups,
        "returned_count": len(results),
        "total_matches": total_matches,
        "has_more": has_more,
        "results": results,
    }
