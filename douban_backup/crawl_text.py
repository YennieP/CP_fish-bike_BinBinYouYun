"""阶段一：顺着“后页”翻页，每页追加写 replies.jsonl。

断点：state.json 里的 cursor 是下一次要抓的页面地址。
走到最后一页（没有“后页”）时，cursor 停在最后一页本身，
所以下次运行会先重抓最后一页拿到新回复，再继续往后翻——这就是增量更新。
"""
from __future__ import annotations

import time

from .net import Client
from .parse import canonical_page_url, parse_page
from .store import TopicStore


def run(client: Client, store: TopicStore, max_pages: int, start: int | None = None, log=print):
    state = store.state()
    seen = {r["id"] for r in store.replies()}
    if start is not None:
        url = canonical_page_url(store.topic_id, start)
    else:
        url = state.get("cursor") or canonical_page_url(store.topic_id, 0)

    for _ in range(max_pages):
        html = client.get_page(url)  # 被拦截时抛 Blocked，之前的页都已落盘
        page = parse_page(html, store.topic_id, url)
        store.save_page_html(page["start"], html)

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        new = []
        for r in page["replies"]:
            if r["id"] not in seen:
                seen.add(r["id"])
                r["fetched_at"] = now
                new.append(r)
        store.append_jsonl(store.replies_path, new)
        if page["topic"]:
            page["topic"]["fetched_at"] = now
            store.write_json(store.topic_path, page["topic"])

        state.setdefault("pages", {})[str(page["start"])] = {"count": len(page["replies"]), "fetched_at": now}
        state["total_pages"] = page["total_pages"] or state.get("total_pages")
        state["last_start"] = page["start"]
        state["cursor"] = page["next_url"] or url
        state["reached_end"] = page["next_url"] is None
        store.save_state(state)

        page_no = page["start"] // 100 + 1 if page["start"] % 100 == 0 else "?"
        log(f"第 {page_no}/{state['total_pages']} 页 (start={page['start']})：{len(page['replies'])} 条，新增 {len(new)} 条，累计 {len(seen)} 条")

        if page["next_url"] is None:
            log("已到最后一页。之后再运行会从最后一页开始抓新增回复。")
            break
        url = page["next_url"]
    return state
