"""头像：收集每位发帖人的头像地址并下载。

- 地址来源：回复记录里的 avatar_url；早期抓的记录没有这个字段，就从 pages/ 缓存里补，不重新请求帖子页面。
- 文件按内容的哈希命名（avatars/<哈希>.jpg），文件名里没有用户 ID；头像完全相同的人共用一个文件。
- avatars.json 记录 用户ID -> {url, file}，同时是断点。头像以第一次记录到的为准。
"""
from __future__ import annotations

import glob
import hashlib
import os
import time

from bs4 import BeautifulSoup

from .crawl_images import sniff_ext
from .net import Blocked, Client
from .store import TopicStore


def avatars_path(store: TopicStore) -> str:
    return os.path.join(store.root, "avatars.json")


def avatars_dir(store: TopicStore) -> str:
    d = os.path.join(store.root, "avatars")
    os.makedirs(d, exist_ok=True)
    return d


def load(store: TopicStore):
    return store.read_json(avatars_path(store), {}) or {}


def files_by_user(store: TopicStore):
    """用户ID -> 头像文件名（只含已下载的）。"""
    return {uid: e["file"] for uid, e in load(store).items() if e.get("file")}


def collect(store: TopicStore, log=print):
    table = load(store)
    topic = store.read_json(store.topic_path) or {}
    authors = {}
    if topic.get("author_id"):
        authors[topic["author_id"]] = topic.get("avatar_url")
    for r in store.replies():
        if r.get("author_id"):
            authors.setdefault(r["author_id"], r.get("avatar_url"))
            if not authors[r["author_id"]] and r.get("avatar_url"):
                authors[r["author_id"]] = r["avatar_url"]

    missing = {uid for uid, url in authors.items() if not url and uid not in table}
    if missing:  # 早期记录没有头像字段：从页面缓存里补
        log(f"从页面缓存补 {len(missing)} 位发帖人的头像地址…")
        for f in sorted(glob.glob(os.path.join(store.pages_dir, "*.html"))):
            soup = BeautifulSoup(open(f, encoding="utf-8").read(), "lxml")
            box = soup.select_one("#topic-content")
            if box is not None and topic.get("author_id") in missing:
                img = box.select_one(".user-face img")
                if img is not None:
                    authors[topic["author_id"]] = img.get("src")
            for li in soup.select("#comments > li.comment-item"):
                uid = li.get("data-author-id")
                if uid in missing and not authors.get(uid):
                    img = li.select_one(".user-face img")
                    if img is not None:
                        authors[uid] = img.get("src")

    for uid, url in authors.items():
        if url and uid not in table:
            table[uid] = {"url": url, "file": None}
    store.write_json(avatars_path(store), table)
    return table


def run(client: Client, store: TopicStore, max_n: int | None = None, log=print):
    table = collect(store, log)
    out_dir = avatars_dir(store)
    by_url = {}  # 同一地址只下一次（默认头像很多人共用）
    for e in table.values():
        if e.get("file"):
            by_url[e["url"]] = e["file"]
    todo = [uid for uid, e in table.items() if not e.get("file") and not e.get("failed")]
    log(f"头像：共 {len(table)} 位，已下载 {len(table) - len(todo)} 位，本次待下载 {len(todo)} 位")
    if max_n is not None:
        todo = todo[:max_n]

    for n, uid in enumerate(todo, 1):
        e = table[uid]
        if e["url"] in by_url:
            e["file"] = by_url[e["url"]]
        else:
            status, data = client.get_image(e["url"])
            ext = sniff_ext(data) if status == 200 else None
            if status == 403:
                store.write_json(avatars_path(store), table)
                raise Blocked(f"头像地址返回 403")
            if not ext:
                e["failed"] = status
                log(f"[{n}/{len(todo)}] 头像下载失败 HTTP {status}")
            else:
                name = hashlib.sha1(data).hexdigest()[:16] + "." + ext
                path = os.path.join(out_dir, name)
                if not os.path.exists(path):
                    with open(path + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(path + ".tmp", path)
                e["file"] = name
                by_url[e["url"]] = name
        e["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        store.write_json(avatars_path(store), table)  # 每位都落盘，中断最多重下一张
        if n % 20 == 0 or n == len(todo):
            log(f"[{n}/{len(todo)}] 头像已处理")
    store.write_json(avatars_path(store), table)
