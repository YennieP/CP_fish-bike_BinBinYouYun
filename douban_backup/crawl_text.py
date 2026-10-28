"""阶段一：顺着“后页”翻页，每页追加写 replies.jsonl。

断点：state.json 里的 cursor 是下一次要抓的页面地址。
走到最后一页（没有“后页”）时，cursor 停在最后一页本身，
所以下次运行会先重抓最后一页拿到新回复，再继续往后翻——这就是增量更新。

全量复查（refresh）：从第 1 页重新翻到末页，更新旧楼层的点赞数、楼主赞过和主楼内容，
把这一轮没翻到的回复标记为“原帖已删除”（备份里保留原文）。进度存在 state.json 的
refresh 里，中断后再运行会接着查。replies.jsonl 不改动，变化追加写到 updates.jsonl。
"""
from __future__ import annotations

import bisect
import os
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


REFRESH_FIELDS = ("likes", "op_liked", "is_folded")
# 一轮复查里没翻到的回复超过这个比例，多半是页面出了问题而不是真被删了，不做标记
MAX_MISSING_RATIO = 0.05


def update_topic(store: TopicStore, topic: dict, now: str, log=print):
    """主楼内容有变化时，把旧版本存到 topic_history/ 再覆盖。"""
    old = store.read_json(store.topic_path) or {}
    if old and old.get("content_html") != topic.get("content_html"):
        hist = os.path.join(store.root, "topic_history")
        os.makedirs(hist, exist_ok=True)
        stamp = (old.get("fetched_at") or now).replace(":", "").replace(" ", "-")
        store.write_json(os.path.join(hist, f"topic-{stamp}.json"), old)
        log("  主楼内容有修改，已更新（旧版本存在 topic_history/）")
    topic["fetched_at"] = now
    store.write_json(store.topic_path, topic)


def refresh(client: Client, store: TopicStore, max_pages: int, log=print):
    state = store.state()
    scan = state.get("refresh")
    if scan:
        log(f"继续全量复查 {scan['id']}：已查 {len(scan['pages'])} 页")
    else:
        scan = {"id": time.strftime("%Y%m%d-%H%M"), "next_url": canonical_page_url(store.topic_id, 0),
                "pages": {}, "seen": [], "updated": 0, "new": 0}
        log(f"开始全量复查 {scan['id']}：从第 1 页翻到末页")
    cache_dir = os.path.join(store.pages_dir, f"refresh-{scan['id']}")  # 不覆盖第一次抓到的原始页面
    os.makedirs(cache_dir, exist_ok=True)

    current = {r["id"]: r for r in store.replies()}
    seen = set(scan["seen"])

    def absorb(replies, now):
        """记下这一页翻到的回复：新回复追加，旧回复有变化就写一条更新。"""
        new, patches = [], []
        for r in replies:
            seen.add(r["id"])
            old = current.get(r["id"])
            if old is None:
                r["fetched_at"] = now
                current[r["id"]] = r
                new.append(r)
                continue
            patch = {k: r.get(k) for k in REFRESH_FIELDS if r.get(k) != old.get(k)}
            if old.get("deleted"):
                patch["deleted"] = False  # 上一轮没翻到，这一轮又出现了
            if patch:
                old.update(patch)
                patches.append({"id": r["id"], **patch, "at": now})
        store.append_jsonl(store.replies_path, new)
        store.append_jsonl(store.updates_path, patches)
        scan["new"] += len(new)
        scan["updated"] += len(patches)
        return new, patches

    url = scan["next_url"]
    for _ in range(max_pages):
        html = client.get_page(url)  # 被拦截时抛 Blocked，已查的页都已落盘
        page = parse_page(html, store.topic_id, url)
        with open(os.path.join(cache_dir, f"start_{page['start']:06d}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        new, patches = absorb(page["replies"], now)
        if page["topic"]:
            update_topic(store, page["topic"], now, log)

        scan["pages"][str(page["start"])] = {"count": len(page["replies"]), "fetched_at": now}
        scan["seen"] = sorted(seen, key=int)
        scan["next_url"] = page["next_url"]
        state["total_pages"] = page["total_pages"] or state.get("total_pages")
        state["refresh"] = scan
        store.save_state(state)
        log(f"复查 {len(scan['pages'])}/{state['total_pages']} 页 (start={page['start']})："
            f"{len(page['replies'])} 条，有变化 {len(patches)} 条，新增 {len(new)} 条")

        if page["next_url"] is None:
            return _finish_refresh(client, store, state, scan, current, seen, absorb, url, page, log)
        url = page["next_url"]
    log("本次页数用完，复查还没结束；再运行 text --refresh 会接着查。")
    return state


def _finish_refresh(client, store, state, scan, current, seen, absorb, last_url, last_page, log):
    tid = store.topic_id
    missing = [i for i, r in current.items() if i not in seen and not r.get("deleted")]
    if missing:
        # 翻页途中前面若有回复被删，后面的回复会整体前移，可能刚好跳过一条。
        # 按每条没翻到的回复现在应在的位置，再看一遍那一页（靠近页边时连同相邻页），确认真的不在了。
        live = sorted(int(i) for i in seen)
        starts = set()
        for m in missing:
            pos = bisect.bisect_left(live, int(m))
            s = pos // 100 * 100
            starts.add(s)
            if pos % 100 < 5 and s:
                starts.add(s - 100)
            if pos % 100 > 94:
                starts.add(s + 100)
        log(f"有 {len(missing)} 条回复这一轮没翻到，再核对 {len(starts)} 页……")
        for s in sorted(starts):
            u = canonical_page_url(tid, s)
            page = parse_page(client.get_page(u), tid, u)
            absorb(page["replies"], time.strftime("%Y-%m-%d %H:%M:%S"))
        missing = [m for m in missing if m not in seen]

    if len(missing) > MAX_MISSING_RATIO * max(len(current), 1):
        path = os.path.join(store.root, f"refresh-{scan['id']}-missing.json")
        store.write_json(path, sorted(missing, key=int))
        log(f"⚠️ 有 {len(missing)} 条回复没翻到，比例太高，可能是页面异常而不是被删，这次不做“已删除”标记。"
            f"名单存在 {path}。点赞和主楼的更新已保存。")
        missing = []

    deleted_at = time.strftime("%Y-%m-%d %H:%M:%S")
    store.append_jsonl(store.updates_path,
                       [{"id": m, "deleted": True, "deleted_at": deleted_at, "at": deleted_at} for m in missing])

    state["pages"] = scan["pages"]
    state["last_start"] = last_page["start"]
    state["cursor"] = last_url
    state["reached_end"] = True
    state["last_refresh"] = {"id": scan["id"], "finished_at": deleted_at, "updated": scan["updated"],
                             "new": scan["new"], "deleted": len(missing)}
    state.pop("refresh", None)
    store.save_state(state)
    log(f"全量复查完成：{scan['updated']} 条旧回复有变化，新增 {scan['new']} 条，新标记已删除 {len(missing)} 条。")
    return state
