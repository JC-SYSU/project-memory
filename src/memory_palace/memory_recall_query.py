"""按需 Recall 查询包装，固定使用 active 与最多 10 条结果。"""

from .memory_recall import recall_memories_with_total


def query_memory_recall(database_path, *, project_id, query):
    """执行一次固定范围的 Recall 查询并返回稳定响应契约。"""
    results, total_matches = recall_memories_with_total(
        database_path,
        project_id=project_id,
        query=query,
        limit=10,
        status_scope="active",
    )
    returned_count = len(results)
    return {
        "query": query,
        "returned_count": returned_count,
        "total_matches": total_matches,
        "has_more": total_matches > returned_count,
        "results": results,
    }
