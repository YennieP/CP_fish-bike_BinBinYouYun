"""一键增量更新：补数据 → 生成存档版、分享版、手机版 → 整理上传文件夹，最后列出这次要发给朋友的文件。

  1. text        从断点翻到末页（--refresh 时改为全量复查）
  2. images      只下新图
  3. avatars     只下新头像
  4. render      存档版
  5. share       分享版完整包 + 更新包（和上一个版本比较）
  6. epub        全本 + 需要重新生成的分册（按上个版本的楼数算出来）
  7. bundle      上传文件夹

和上个版本相比没有变化时，在第 4 步之前停下，不产生空版本（空版本会让更新包链条多出一环）。
被风控拦下时抛 Blocked，由 backup.py 提示并退出；已生成的成品不受影响，下次重新运行即可。
"""
from __future__ import annotations

import math
import os

from . import avatars, bundle, crawl_images, crawl_text, epub, share
from .net import Client
from .render import Renderer
from .store import TopicStore


def step(log, n, text):
    log(f"\n—— {n}/7 {text} ——")


def volumes_to_rebuild(prev_floors: int, floors: int, per_book: int, have_parts: bool):
    """上个版本的最后一册到现在的最后一册都要重新生成（跨过整册时，上一册会补满、并新开一册）。"""
    total = max(1, math.ceil(floors / per_book))
    if not have_parts or not prev_floors:
        return list(range(1, total + 1))
    first = max(1, math.ceil(prev_floors / per_book))
    return list(range(first, total + 1))


def run(store: TopicStore, refresh: bool = False, fetch: bool = True, per_book: int = 2500,
        dist_dir: str = "dist", log=print):
    release = store.read_json(os.path.join(store.root, "releases", "latest.json")) or {}
    prev_floors = release.get("floors", 0)
    prev_version = release.get("version")

    if fetch:
        client = Client()
        step(log, 1, "全量复查" if refresh else "补新回复")
        if refresh:
            st = crawl_text.refresh(client, store, 10000, log=log)
            if st.get("refresh"):
                raise SystemExit("全量复查没跑完，再运行一次 update --refresh 会接着查")
        else:
            st = crawl_text.run(client, store, 10000, log=log)
            if not st.get("reached_end"):
                raise SystemExit("还没翻到最后一页，再运行一次 update 会接着翻")
        step(log, 2, "下载新图片")
        crawl_images.run(client, store, log=log)
        step(log, 3, "下载新头像")
        avatars.run(client, store, log=log)
    else:
        log("（跳过联网，只用已有数据重新生成）")

    floors = len(store.replies())
    done, wanted = len(crawl_images.done_map(store)), len(crawl_images.collect_images(store))
    if done < wanted:
        log(f"⚠️ 还有 {wanted - done} 张图没下载成功，生成的成品里会显示“未下载”。")
    if not refresh and prev_version and floors == prev_floors:
        log(f"\n和上个版本 {prev_version} 相比没有新回复，不生成新版本。")
        return None

    step(log, 4, "生成存档版")
    Renderer(store).run(log)
    step(log, 5, "生成分享版")
    share.build(store, dist_dir, log=log)
    release = store.read_json(os.path.join(store.root, "releases", "latest.json"))
    version = release["version"]

    step(log, 6, "生成手机版")
    epub.build(store, dist_dir, full=True, log=log)
    epub_dir = os.path.join(dist_dir, f"epub-{version}")
    have_parts = os.path.isdir(epub.parts_dir(epub_dir, per_book)) and bool(os.listdir(epub.parts_dir(epub_dir, per_book)))
    vols = volumes_to_rebuild(prev_floors, floors, per_book, have_parts and not refresh)
    epub.build(store, dist_dir, volumes=vols, per_book=per_book, log=log)

    step(log, 7, "整理上传文件夹")
    out = bundle.build(store, dist_dir, log=log)

    tid = store.topic_id
    upd = f"douban-{tid}-update-{prev_version}-to-{version}.zip"
    parts = sorted(f for f in os.listdir(epub.parts_dir(epub_dir, per_book)) if f.endswith(".epub"))
    changed_parts = [p for p in parts if int(p.split(f"douban-{tid}-")[1][:2]) in vols]
    log(f"\n✅ 新版本 {version}：{prev_floors} → {floors} 楼" + ("（含全量复查）" if refresh else ""))
    log(f"上传文件夹：{out}")
    log("这次要告诉朋友的：")
    if prev_version and os.path.exists(os.path.join(dist_dir, upd)):
        log(f"  · 电脑版：已有旧版本的，只下载更新包 {upd}；新朋友下载完整包")
    else:
        log("  · 电脑版：下载完整包")
    log("  · 手机版全本：重新下载整本")
    if len(changed_parts) == len(parts):
        log(f"  · 手机版分册：全部 {len(parts)} 册都要重新下载")
    else:
        log(f"  · 手机版分册：只需重新下载 {'、'.join(changed_parts)}")
    return out
