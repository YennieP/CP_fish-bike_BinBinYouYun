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

ENTRY = "点我打开.html"   # 分享包最外层只放入口、说明和 内容/，朋友一打开就知道点哪个
GUIDE_TXT = "使用说明.txt"
PAGES_DIR = "内容"

MAX_SIDE = 1280
JPEG_QUALITY = 80

README = """\
豆瓣帖子「{title}」离线备份 · 电脑版
================================================

版本：{version}
收录到 {last_time}（北京时间），共 {floors} 楼（不含主楼）。

这份说明只讲电脑版（就是这个文件夹）。
手机版 EPUB 怎么下载、怎么打开，见网盘里的“00-先看我-下载说明.txt”。


一、怎么打开
------------
双击这个文件夹里的“点我打开.html”，会用浏览器（Chrome / Edge / Safari）打开目录页。
不需要联网，也不需要安装任何东西。

  这个文件夹里只有三样东西：
    点我打开.html   入口，从这里开始看
    使用说明.txt    就是这份说明
    内容/           全部楼层和图片都在这里，不用管它

  · 每 500 楼一个分卷，在目录页里点进去。
  · 想直接跳到某一楼：在页面顶部的“跳到楼层”框里输入楼层号（例如 1234），点“跳转”。
  · 动图已转成小视频，滚动到时会自动循环播放，效果和动图一样。


二、页面上的标记是什么意思
--------------------------
  momo·16号       昵称后面的“·N号”是本备份给每位发帖人编的序号，用来区分同名的人
                  （楼里有很多叫 momo 的），和豆瓣账号无关。以后更新，老用户的号码不变。
  豆友·25号       没设昵称的人。豆瓣上显示为“豆友+一串数字”，这里去掉了数字。
  楼主、楼主赞过   和豆瓣上一样。
  原帖已删除       这条回复在豆瓣上已经被删了，这里保留了原文。
  引用里的 #24     点它会跳到被引用的第 24 楼。


三、怎么更新到新版本
--------------------
楼里有新回复时，网盘里会放上新版本。先看本说明最上面的“版本”，这是你现在的版本号。

办法 1：重新下载完整包（最简单，Windows、Mac 都适用）
  1. 下载文件名带 full 的完整包，例如 {folder}-full-20261105-2100.zip；
  2. 解压，得到新的 {folder} 文件夹；
  3. 把旧的 {folder} 文件夹删掉，换成新的。

办法 2：只下载更新包（只适合 Windows，并且只差一个版本时）
  更新包的文件名形如 {folder}-update-{version}-to-20261105-2100.zip，
  意思是“把 {version} 版升级到 20261105-2100 版”，只含有变化的文件，通常只有几 MB。
  只有当你现在的版本号，正好等于更新包名字里 -update- 后面的那个版本号时，才能用它。

  例子：你的电脑版放在“下载\\{folder}”里，
    1. 右键更新包 →“全部解压缩”，目标文件夹选“下载”
       （选它的上一层，不要选 {folder} 里面，因为更新包里已经自带了 {folder} 这一层）；
    2. 问是否替换文件时，选“替换目标中的文件”；
    3. 再打开这份“使用说明.txt”，最上面的版本号变成新的，就说明更新成功了。

  Mac 用户请用办法 1：Mac 双击解压不会覆盖旧文件，只会在旁边多出一个“{folder} 2”。
  （会用“终端”的话，也可以在上一层文件夹里运行：ditto -x -k 更新包文件名.zip . ）

  版本号对不上（比如中间漏过一次更新），也请用办法 1。


备份内容的版权属于原发帖人，只在本楼内分享，请不要公开转载。
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


def prune_zips(dist_dir: str, topic_id: str, version: str, log=print):
    """dist 里只留本版本的完整包和“上一版 → 本版”的更新包，更早的删掉。
    生成更新包靠的是 data/<帖子>/releases/ 里的文件清单，不需要旧 zip。"""
    keep = {f"douban-{topic_id}-full-{version}.zip"}
    keep |= {f for f in os.listdir(dist_dir) if f.startswith(f"douban-{topic_id}-update-") and f.endswith(f"-to-{version}.zip")}
    for f in sorted(os.listdir(dist_dir)):
        if f.endswith(".zip") and f.startswith((f"douban-{topic_id}-full-", f"douban-{topic_id}-update-")) and f not in keep:
            os.remove(os.path.join(dist_dir, f))
            log(f"  删除旧压缩包 {f}")


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
    pages = os.path.join(pkg, PAGES_DIR)
    pkg_media = os.path.join(pages, "media")
    os.makedirs(pages, exist_ok=True)
    if os.path.isdir(os.path.join(pkg, "media")) and not os.path.exists(pkg_media):
        os.rename(os.path.join(pkg, "media"), pkg_media)  # 旧版结构：素材挪进 内容/ 接着用，不用重新复制
    os.makedirs(pkg_media, exist_ok=True)
    for d in (pkg, pages):  # 分卷、入口和说明都会重新生成；顺带清掉旧版结构留在最外层的文件
        for f in os.listdir(d):
            if f.endswith((".html", ".txt")):
                os.remove(os.path.join(d, f))
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
                 avatar_prefix="media/avatars/", share=True, entry=ENTRY, pages_dir=PAGES_DIR)
    r.run(log)

    last_time = r.replies[-1]["pubtime"] if r.replies else "-"
    with open(os.path.join(pkg, GUIDE_TXT), "w", encoding="utf-8") as f:
        f.write(README.format(title=r.topic.get("title", ""), last_time=last_time,
                              floors=len(r.replies), version=version, folder=top))

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
        removed = [p for p in prev if p not in cur]
        if removed:
            # 更新包只能新增、覆盖文件，删不掉旧文件；有文件被删或改名时（例如调整了文件夹结构），
            # 朋友装更新包后会留下旧文件，所以这次不生成更新包，请大家重新下载完整包
            log(f"  有 {len(removed)} 个文件被删除或改名（例如 {removed[0]}），更新包处理不了，这次不生成更新包；"
                "请大家重新下载完整包")
        elif changed:
            since = (store.read_json(latest_path, {}) or {}).get("version", "?")
            write_zip(os.path.join(dist_dir, f"douban-{store.topic_id}-update-{since}-to-{version}.zip"),
                      pkg, changed, log, top)
        else:
            log("  和上次发布相比没有变化，不生成更新包")

    release = {"version": version, "floors": len(r.replies), "last_time": last_time, "files": cur}
    store.write_json(os.path.join(releases, f"{version}.json"), release)
    store.write_json(latest_path, release)
    prune_zips(dist_dir, store.topic_id, version, log)  # 新版本记录好之后再删旧包
    log(f"版本 {version}，收录到 {last_time}，共 {len(r.replies)} 楼")
    return pkg
