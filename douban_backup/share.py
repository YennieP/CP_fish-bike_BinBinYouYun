"""分享版：压缩素材 → 渲染到分享包目录 → 打完整包 + 更新包。

目录：
  data/<帖子>/share_media/   压缩后的素材缓存（GIF→MP4、大图缩小），只转换新增的
  data/<帖子>/releases/      每次发布的文件清单（用来算更新包里该放哪些文件）
  dist/douban-<帖子>-<版本>/ 分享包文件夹（压缩包里固定叫 douban-<帖子>/，就是朋友解压后看到的样子）
  dist/*.zip                 完整包、更新包
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile

from .crawl_images import done_map
from .render import Renderer
from .store import TopicStore

MAX_SIDE = 1280
JPEG_QUALITY = 80

README = """\
豆瓣帖子「{title}」离线备份
================================

打开方法：双击本文件夹里的 index.html，用浏览器（Chrome / Edge / Safari）打开即可。
不需要联网，也不需要安装任何东西。

- 收录到 {last_time}，共 {floors} 楼（不含主楼）。
- 每 500 楼一个分卷；每页顶部可以输入楼层号直接跳转。
- 动图已转成体积更小的视频，滚动到时会自动循环播放，效果和动图一样。
- 昵称后面的“·N号”是本备份给每位发帖人编的序号，用来区分同名用户，和豆瓣账号无关。

更新：收到“更新包”（文件名带 update）时，把它解压到和本文件夹相同的位置，
选择“覆盖 / 替换”即可。不需要重新下载完整包。

版本：{version}
"""


# ---------------- 素材压缩 ----------------

def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def gif_to_mp4(src: str, dst: str) -> bool:
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-i", src, "-an", "-movflags", "+faststart",
           "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
           "-c:v", "libx264", "-crf", "26", "-preset", "slow", dst + ".tmp.mp4"]
    ok = subprocess.run(cmd, capture_output=True).returncode == 0
    if ok:
        os.replace(dst + ".tmp.mp4", dst)
    elif os.path.exists(dst + ".tmp.mp4"):
        os.remove(dst + ".tmp.mp4")
    return ok


def shrink_image(src: str, out_dir: str, pid: str) -> str:
    """长边缩到 MAX_SIDE；不透明的图存成 JPEG。压缩后反而更大就用原图。返回输出文件名。"""
    from PIL import Image

    ext = src.rsplit(".", 1)[-1]
    with Image.open(src) as im:
        if getattr(im, "is_animated", False):  # 动态 webp/png：原样保留
            name = f"p{pid}.{ext}"
            shutil.copyfile(src, os.path.join(out_dir, name))
            return name
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        if has_alpha:
            name, fmt, kw = f"p{pid}.png", "PNG", {"optimize": True}
        else:
            im = im.convert("RGB")
            name, fmt, kw = f"p{pid}.jpg", "JPEG", {"quality": JPEG_QUALITY, "progressive": True, "optimize": True}
        dst = os.path.join(out_dir, name)
        im.save(dst + ".tmp", fmt, **kw)
    if os.path.getsize(dst + ".tmp") >= os.path.getsize(src) and name.endswith("." + ext):
        os.remove(dst + ".tmp")
        shutil.copyfile(src, dst)
    else:
        os.replace(dst + ".tmp", dst)
    return name


def prepare_media(store: TopicStore, log=print):
    """返回 pid -> {"img": ..., "video": ...}（文件名，位于 share_media/）。已转换过的直接复用。"""
    out_dir = os.path.join(store.root, "share_media")
    os.makedirs(out_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "index.json")
    index = store.read_json(index_path, {}) or {}

    done = done_map(store)
    todo = [pid for pid in done if pid not in index]
    log(f"素材：共 {len(done)} 个，已压缩 {len(done) - len(todo)} 个，本次处理 {len(todo)} 个")
    for n, pid in enumerate(todo, 1):
        rec = done[pid]
        src = os.path.join(store.images_dir, rec["file"])
        entry = {}
        if rec.get("video"):  # 只有视频版的动图表情：直接用视频
            name = f"p{pid}.mp4"
            shutil.copyfile(os.path.join(store.images_dir, rec["video"]), os.path.join(out_dir, name))
            entry["video"] = name
        elif rec["format"] == "gif":
            name = f"p{pid}.mp4"
            if gif_to_mp4(src, os.path.join(out_dir, name)):
                entry["video"] = name
            else:  # 极少数转不了的 GIF：原样保留
                shutil.copyfile(src, os.path.join(out_dir, f"p{pid}.gif"))
                entry["img"] = f"p{pid}.gif"
                log(f"  p{pid}.gif 转视频失败，保留原 GIF")
        else:
            entry["img"] = shrink_image(src, out_dir, pid)
        index[pid] = entry
        if n % 50 == 0 or n == len(todo):
            store.write_json(index_path, index)
            log(f"  已处理 {n}/{len(todo)}")
    store.write_json(index_path, index)
    return index, out_dir


# ---------------- 打包 ----------------

def sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_of(pkg: str):
    out = {}
    for root, _, files in os.walk(pkg):
        for fn in files:
            if fn.startswith("."):
                continue
            full = os.path.join(root, fn)
            out[os.path.relpath(full, pkg)] = sha1(full)
    return out


def adopt_dir(dist_dir: str, prefix: str, version: str, log=print) -> str:
    """输出文件夹名带上版本时间：<prefix>-<版本>。把上一次的同类文件夹改名过来接着用
    （素材只补新增的、只重新生成变化的分册），所以 dist 里每类只保留最新的一份。"""
    target = os.path.join(dist_dir, f"{prefix}-{version}")
    os.makedirs(dist_dir, exist_ok=True)
    old = [d for d in os.listdir(dist_dir) if os.path.isdir(os.path.join(dist_dir, d))
           and (d == prefix or re.fullmatch(re.escape(prefix) + r"-\d{8}-\d{4}", d))]
    old = [d for d in old if d != os.path.basename(target)]
    if old and not os.path.exists(target):
        newest = max(old, key=lambda d: os.path.getmtime(os.path.join(dist_dir, d)))
        os.rename(os.path.join(dist_dir, newest), target)
        log(f"  {newest}/ → {os.path.basename(target)}/")
        old.remove(newest)
    for d in old:
        log(f"  注意：{d}/ 是更早的旧文件夹，确认不用后可以删掉")
    os.makedirs(target, exist_ok=True)
    return target


def write_zip(zip_path: str, pkg: str, rel_paths, log, top: str):
    """top 是朋友解压后看到的文件夹名，固定不带版本，更新包才能覆盖到同一个文件夹里。"""
    tmp = zip_path + ".tmp"
    with zipfile.ZipFile(tmp, "w") as z:
        for rel in sorted(rel_paths):
            # 图片和视频本身已压缩，直接存；HTML 文本用 deflate
            ctype = zipfile.ZIP_DEFLATED if rel.endswith((".html", ".txt")) else zipfile.ZIP_STORED
            z.write(os.path.join(pkg, rel), f"{top}/{rel}", compress_type=ctype)
    os.replace(tmp, zip_path)
    log(f"  {os.path.basename(zip_path)}：{len(rel_paths)} 个文件，{os.path.getsize(zip_path) / 1024 / 1024:.0f} MB")


def build(store: TopicStore, dist_dir: str = "dist", make_zip: bool = True, log=print):
    media, media_dir = prepare_media(store, log)

    version = time.strftime("%Y%m%d-%H%M")
    top = f"douban-{store.topic_id}"
    pkg = adopt_dir(dist_dir, top, version, log)
    pkg_media = os.path.join(pkg, "media")
    os.makedirs(pkg_media, exist_ok=True)
    for f in os.listdir(pkg):  # 分卷会重新生成
        if f.endswith(".html"):
            os.remove(os.path.join(pkg, f))
    wanted = {f for e in media.values() for f in e.values()}
    for f in wanted:
        dst = os.path.join(pkg_media, f)
        if not os.path.exists(dst):
            shutil.copyfile(os.path.join(media_dir, f), dst)
    for f in os.listdir(pkg_media):
        if f not in wanted and os.path.isfile(os.path.join(pkg_media, f)):
            os.remove(os.path.join(pkg_media, f))

    from .avatars import avatars_dir, files_by_user
    pkg_av = os.path.join(pkg_media, "avatars")
    os.makedirs(pkg_av, exist_ok=True)
    av_wanted = set(files_by_user(store).values())
    for f in av_wanted:
        dst = os.path.join(pkg_av, f)
        if not os.path.exists(dst):
            shutil.copyfile(os.path.join(avatars_dir(store), f), dst)
    for f in os.listdir(pkg_av):
        if f not in av_wanted:
            os.remove(os.path.join(pkg_av, f))

    r = Renderer(store, out_dir=pkg, media=media, media_prefix="media/",
                 avatar_prefix="media/avatars/", share=True)
    r.run(log)

    last_time = r.replies[-1]["pubtime"] if r.replies else "-"
    with open(os.path.join(pkg, "README.txt"), "w", encoding="utf-8") as f:
        f.write(README.format(title=r.topic.get("title", ""), last_time=last_time,
                              floors=len(r.replies), version=version))

    if not make_zip:
        log(f"分享包文件夹：{pkg}")
        return pkg

    releases = os.path.join(store.root, "releases")
    os.makedirs(releases, exist_ok=True)
    latest_path = os.path.join(releases, "latest.json")
    prev = (store.read_json(latest_path, {}) or {}).get("files", {})
    cur = manifest_of(pkg)

    log("打包：")
    write_zip(os.path.join(dist_dir, f"douban-{store.topic_id}-full-{version}.zip"), pkg, cur.keys(), log, top)
    if prev:
        changed = [p for p, h in cur.items() if prev.get(p) != h]
        if changed:
            since = (store.read_json(latest_path, {}) or {}).get("version", "?")
            write_zip(os.path.join(dist_dir, f"douban-{store.topic_id}-update-{since}-to-{version}.zip"),
                      pkg, changed, log, top)
        else:
            log("  和上次发布相比没有变化，不生成更新包")

    release = {"version": version, "floors": len(r.replies), "last_time": last_time, "files": cur}
    store.write_json(os.path.join(releases, f"{version}.json"), release)
    store.write_json(latest_path, release)
    log(f"版本 {version}，收录到 {last_time}，共 {len(r.replies)} 楼")
    return pkg
