"""阶段二：下载回复、引用和主楼里的图片。images.jsonl 记录每张图的结果，同时是断点。

- 按图片编号去重：同一张图在原回复和引用里只下一次。
- 按文件头判断格式（豆瓣把 GIF 也标成 .jpg / image/jpeg），GIF 存成 .gif。
- 每张图依次尝试候选地址：带签名的原图 → 无签名 raw → large。
  所有候选都 403 时视为被风控，立刻停止。
"""
from __future__ import annotations

import os
import re
import time

from .net import Blocked, Client
from .store import TopicStore


def sniff_ext(data: bytes):
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def collect_images(store: TopicStore):
    """按出现顺序收集所有图片，按 pid 去重。"""
    found = {}
    topic = store.read_json(store.topic_path)
    for img in (topic or {}).get("images", []):
        found.setdefault(img["pid"], img)
    for r in sorted(store.replies(), key=lambda r: int(r["id"])):
        for img in (r.get("image"), (r.get("quote") or {}).get("image")):
            if not img:
                continue
            prev = found.get(img["pid"])
            if prev is None:
                found[img["pid"]] = img
            else:  # 合并候选地址（引用里也可能带签名地址）
                for u in img["candidates"]:
                    if u not in prev["candidates"]:
                        prev["candidates"].append(u)
                prev["is_gif"] = prev["is_gif"] or img["is_gif"]
    return list(found.values())


def done_map(store: TopicStore):
    """pid -> 最新一条成功记录。"""
    out = {}
    for rec in store.read_jsonl(store.images_log):
        if rec.get("status") == "ok":
            out[rec["pid"]] = rec
    return out


def video_candidates(img):
    v = img.get("video_url")
    if not v:
        return []
    m = re.search(r"(/view/[^/]+/raw/public/p\d+\.mp4)", v)
    return [v] + (["https://img3.doubanio.com" + m.group(1)] if m else [])


def fetch_video(client: Client, store: TopicStore, img, rec, log):
    """有些动图表情的原文件只是静态首帧，动画只以 MP4 形式存在：补下 MP4。"""
    statuses = []
    for url in video_candidates(img):
        status, data = client.get_image(url)
        statuses.append(status)
        if status == 200 and data[4:8] == b"ftyp":
            fname = f"p{img['pid']}.mp4"
            path = os.path.join(store.images_dir, fname)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
            rec.update(video=fname, video_bytes=len(data))
            log(f"    ↳ 补下动图视频 {fname} {len(data) // 1024}KB")
            break
    else:
        if statuses and all(s == 403 for s in statuses):
            raise Blocked(f"图片 p{img['pid']} 的视频地址都返回 403")
        rec["video_failed"] = statuses
        log(f"    ↳ 动图视频下载失败 {statuses}")
    rec["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    store.append_jsonl(store.images_log, [rec])


def run(client: Client, store: TopicStore, max_images: int | None = None, log=print):
    images = collect_images(store)
    done = done_map(store)
    todo = [i for i in images if i["pid"] not in done]
    log(f"共 {len(images)} 张图，已下载 {len(images) - len(todo)} 张，本次待下载 {len(todo)} 张")
    if max_images is not None:
        todo = todo[:max_images]

    # 之前下过、但动图只拿到静态图且还没试过 MP4 的，先补
    for img in images:
        rec = done.get(img["pid"])
        if rec and rec.get("degraded") and "video" not in rec and "video_failed" not in rec and img.get("video_url"):
            log(f"p{img['pid']} 之前只拿到静态图，补下视频")
            fetch_video(client, store, img, dict(rec), log)

    for n, img in enumerate(todo, 1):
        statuses, result = [], None
        for url in img["candidates"]:
            status, data = client.get_image(url)
            statuses.append(status)
            ext = sniff_ext(data) if status == 200 else None
            if ext:
                result = (url, data, ext)
                break
            if status == 200 and data[:1] == b"<":
                raise Blocked(f"图片地址返回了网页而不是图片：{url}")

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if result is None:
            if statuses and all(s == 403 for s in statuses):
                raise Blocked(f"图片 p{img['pid']} 所有地址都返回 403")
            store.append_jsonl(store.images_log, [{"pid": img["pid"], "status": "failed", "http": statuses, "at": now}])
            log(f"[{n}/{len(todo)}] p{img['pid']} 下载失败 {statuses}")
            continue

        url, data, ext = result
        fname = f"p{img['pid']}.{ext}"
        path = os.path.join(store.images_dir, fname)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        degraded = img["is_gif"] and ext != "gif"  # 应是动图却只拿到静态图
        rec = {
            "pid": img["pid"], "status": "ok", "file": fname, "bytes": len(data), "format": ext,
            "expected_gif": img["is_gif"], "degraded": degraded, "url": url.split("?")[0], "at": now,
        }
        flag = "  （原文件是静态图，动画只有视频版）" if degraded else ""
        log(f"[{n}/{len(todo)}] p{img['pid']}.{ext} {len(data) // 1024}KB{flag}")
        if degraded and img.get("video_url"):
            fetch_video(client, store, img, rec, log)
        else:
            store.append_jsonl(store.images_log, [rec])
