"""只读的实时进度面板：定时读取 state.json / jsonl，画进度条。不发任何网络请求。"""
from __future__ import annotations

import os
import sys
import time

from .crawl_images import collect_images
from .store import TopicStore

WIDTH = 30


def bar(done: int, total: int) -> str:
    total = max(total, 1)
    filled = min(WIDTH, round(WIDTH * done / total))
    return f"[{'█' * filled}{'░' * (WIDTH - filled)}] {done}/{total} {100 * done / total:5.1f}%"


def eta(times, remaining: int, unit: str) -> str:
    """用最近 10 分钟的完成时间估算剩余时长。"""
    now = time.time()
    recent = [t for t in times if now - t < 600]
    if remaining <= 0:
        return "已完成"
    if len(recent) < 2:
        return "未在运行" if not times or now - max(times) > 120 else "估算中…"
    rate = (len(recent) - 1) / max(recent[-1] - recent[0], 1)
    if now - max(times) > 120:
        return "未在运行"
    return f"约 {remaining / rate / 60:.0f} 分钟（{rate * 60:.1f} {unit}/分钟）"


def ts(s: str) -> float:
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return 0.0


def snapshot(store: TopicStore) -> str:
    st = store.state()
    pages = st.get("pages", {})
    total_pages = st.get("total_pages") or 0
    page_times = sorted(ts(p.get("fetched_at")) for p in pages.values())
    replies = store.replies()

    images = collect_images(store)
    logs = store.read_jsonl(store.images_log)
    ok = {r["pid"] for r in logs if r.get("status") == "ok"}
    failed = {r["pid"] for r in logs if r.get("status") == "failed"} - ok
    img_times = sorted(ts(r.get("at")) for r in logs if r.get("status") == "ok")
    img_bytes = sum(os.path.getsize(os.path.join(store.images_dir, f))
                    for f in os.listdir(store.images_dir) if not f.endswith(".tmp"))
    gifs = sum(1 for r in logs if r.get("status") == "ok" and r.get("format") == "gif")

    last_page = time.strftime("%H:%M:%S", time.localtime(page_times[-1])) if page_times else "-"
    lines = [
        f"豆瓣帖子 {store.topic_id} 备份进度        {time.strftime('%H:%M:%S')}（每 3 秒刷新，Ctrl+C 退出）",
        "",
        f"文字  {bar(len(pages), total_pages)} 页",
        f"      已存 {len(replies)} 条回复 · 最近一页 {last_page} · 剩余 {eta(page_times, total_pages - len(pages), '页')}",
        "      " + ("已到末页（之后运行只抓增量）" if st.get("reached_end") else f"下一页 start={st.get('cursor', '').split('start=')[-1] or 0}"),
        "",
        f"图片  {bar(len(ok), len(images))} 张",
        f"      GIF {gifs} 张 · 失败 {len(failed)} 张 · 占用 {img_bytes / 1024 / 1024:.0f} MB · 剩余 {eta(img_times, len(images) - len(ok), '张')}",
        "      （图片总数随文字抓取增加而增加）",
    ]
    av = store.read_json(os.path.join(store.root, "avatars.json"), {}) or {}
    if av:
        av_done = sum(1 for e in av.values() if e.get("file") or e.get("failed"))
        av_times = sorted(ts(e.get("at")) for e in av.values() if e.get("at"))
        lines += ["", f"头像  {bar(av_done, len(av))} 位",
                  f"      剩余 {eta(av_times, len(av) - av_done, '位')}（在图片下完后开始）"]
    return "\n".join(lines)


def watch(store: TopicStore, interval: float = 3.0):
    try:
        while True:
            out = snapshot(store)
            sys.stdout.write("\033[H\033[2J" + out + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
