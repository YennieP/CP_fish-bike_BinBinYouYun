"""整理上传网盘用的文件夹：电脑版完整包（和更新包）、手机版全本、手机版分册，外加一份下载说明。

文件用硬链接放进去，不额外占空间。打包前检查这些文件是不是用最新的数据生成的。

  dist/upload/<帖子标题>-离线备份-<版本>/
    00-先看我-下载说明.txt
    电脑版/          douban-<帖子>-full-<版本>.zip（有更新包时一并放入）
    手机版-全本/      douban-<帖子>-全本.epub          （来自 dist/epub-<版本>/全本/）
    手机版-分N册/     douban-<帖子>-01（1-2500楼）.epub ……（来自 dist/epub-<版本>/分册-每N楼/）
    手机版-分N册-打包/ douban-<帖子>-手机版-分册-每N楼.zip（全部分册打成一个包，方便一次下载）
"""
from __future__ import annotations

import os
import re
import shutil
import zipfile

from . import epub
from .store import TopicStore

GUIDE = """\
{title} · 离线备份（版本 {version}）
收录到 {last_time}，共 {floors} 楼（不含主楼）。不需要联网，也不需要装任何软件。

按自己的设备挑一种下载就行，不用全下：

【电脑】电脑版/ 里的 zip（{full_size}）
  下载后解压，双击文件夹里的 index.html，用浏览器打开。
  动图会在滚动到时自动播放；每页顶部可以输入楼层号直接跳转。
{update_note}
【手机】先把文件下载到手机，再用“其他应用打开”，选一个电子书阅读器。
  已在 iPhone 的 Apple Books（图书）上试过；微信读书、多看、静读天下等也可以试试，效果可能不同。
  请先下载再打开，网盘里直接预览多半显示不全。
  · 手机版-全本/（{book_size}）：整楼一个文件，引用的楼层都能点过去。翻页流畅，跨章节跳转会卡一下。
  · 手机版-分{n_parts}册/（每册 {part_range}）：文件小，跳转更快；引用到别册的楼层只显示楼层号。
    以后更新时，只有最后一册会变，前面几册不用重新下载。
    想一次下完全部分册，可以下载 手机版-分{n_parts}册-打包/ 里的 zip（{parts_zip_size}），解压后得到同样的 {n_parts} 个文件。

昵称后面的“·N号”是本备份给每位发帖人编的序号，用来区分同名用户，和豆瓣账号无关。
备份内容的版权属于原发帖人，只在本楼内分享，请不要公开转载。
"""


def mb(n: int) -> str:
    return f"{n / 1024 / 1024 / 1024:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 / 1024:.0f} MB"


def place(src: str, dst_dir: str):
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(src))
    try:
        os.link(src, dst)  # 同一块磁盘上不占额外空间
    except OSError:
        shutil.copyfile(src, dst)


def build(store: TopicStore, dist_dir: str = "dist", log=print):
    tid = store.topic_id
    release = store.read_json(os.path.join(store.root, "releases", "latest.json"))
    if not release:
        raise SystemExit("还没有分享版：先运行 share")
    version = release["version"]
    full_zip = os.path.join(dist_dir, f"douban-{tid}-full-{version}.zip")
    update_zips = sorted(f for f in os.listdir(dist_dir) if f.startswith(f"douban-{tid}-update-") and f.endswith(f"-to-{version}.zip"))
    epub_dir = os.path.join(dist_dir, f"epub-{version}")
    if not os.path.isdir(epub_dir):
        raise SystemExit(f"找不到 {epub_dir}：手机版要在本次 share 之后重新生成（epub --full 和 epub --per-book 2500）")
    book = os.path.join(epub.book_dir(epub_dir), f"douban-{tid}-全本.epub")
    part_dirs = sorted(d for d in (os.listdir(epub_dir) if os.path.isdir(epub_dir) else [])
                       if re.fullmatch(r"分册-每\d+楼", d))
    if len(part_dirs) > 1:
        raise SystemExit(f"{epub_dir} 里有多种分册（{'、'.join(part_dirs)}），请删掉不用的那种再整理")
    src_dir = os.path.join(epub_dir, part_dirs[0]) if part_dirs else ""
    parts = sorted(os.path.join(src_dir, f) for f in (os.listdir(src_dir) if src_dir else [])
                   if re.fullmatch(rf"douban-{tid}-\d\d（\d+-\d+楼）\.epub", f))

    per_book = int(re.search(r"\d+", part_dirs[0]).group()) if part_dirs else 0
    pzip = epub.parts_zip(store, epub_dir, per_book) if part_dirs else ""
    missing = [p for p in (full_zip, book) if not os.path.exists(p)] + ([] if parts else ["手机版分册"])
    if parts and not os.path.exists(pzip):
        missing.append(os.path.basename(pzip) if pzip else "分册 zip")
    if missing:
        raise SystemExit("缺少文件，请先生成：" + "、".join(os.path.basename(m) for m in missing))
    # 分册要连续、最后一册收到最后一楼，否则多半是混进了别的分法生成的旧文件
    spans = [tuple(map(int, re.search(r"（(\d+)-(\d+)楼）", p).groups())) for p in parts]
    floors = release["floors"]
    if spans[0][0] != 1 or spans[-1][1] != floors or any(a[1] + 1 != b[0] for a, b in zip(spans, spans[1:])):
        raise SystemExit(f"{epub_dir} 里的分册不连续或没收到第 {floors} 楼，请重新运行 epub --per-book")

    data_files = [store.replies_path, store.updates_path, store.topic_path]
    newest_data = max(os.path.getmtime(p) for p in data_files if os.path.exists(p))
    # 前面几册的内容不随新回复变化，只要求完整包、全本和最后一册是新的。
    # 全量复查（--refresh）改了旧楼层时，前面几册也会过时，需要整套重新生成。
    stale = [p for p in [full_zip, book, parts[-1]] if os.path.getmtime(p) < newest_data]
    # 分册 zip 必须正好是当前这些分册（只重新生成某几册后会重新打包；这里再核对一遍文件名和大小）
    with zipfile.ZipFile(pzip) as z:
        in_zip = {os.path.basename(i.filename): i.file_size for i in z.infolist()}
    if in_zip != {os.path.basename(p): os.path.getsize(p) for p in parts}:
        raise SystemExit(f"{os.path.basename(pzip)} 和分册文件对不上，请重新运行 epub --per-book {per_book}")
    if stale:
        raise SystemExit("这些文件比最新数据旧，请重新生成后再整理：" + "、".join(os.path.basename(p) for p in stale))

    title = (store.read_json(store.topic_path) or {}).get("title") or tid
    safe = re.sub(r'[\\/:*?"<>|]', "", title).strip()
    out = os.path.join(dist_dir, "upload", f"{safe}-离线备份-{version}")
    if os.path.exists(out):
        shutil.rmtree(out)  # 只含硬链接和说明，删掉不影响 dist 里的原文件
    place(full_zip, os.path.join(out, "电脑版"))
    for u in update_zips:
        place(os.path.join(dist_dir, u), os.path.join(out, "电脑版"))
    place(book, os.path.join(out, "手机版-全本"))
    part_dir = os.path.join(out, f"手机版-分{len(parts)}册")
    for p in parts:
        place(p, part_dir)
    place(pzip, part_dir + "-打包")

    sizes = [os.path.getsize(p) for p in parts]
    update_note = ""
    if update_zips:
        update_note = ("  已经装过旧版本的话，只下载 电脑版/ 里文件名带 update 的更新包，"
                       "解压到原文件夹所在的位置、选择覆盖即可。\n")
    with open(os.path.join(out, "00-先看我-下载说明.txt"), "w", encoding="utf-8") as f:
        f.write(GUIDE.format(title=title, version=version, last_time=release["last_time"], floors=floors,
                             full_size=mb(os.path.getsize(full_zip)), update_note=update_note,
                             book_size=mb(os.path.getsize(book)), n_parts=len(parts),
                             part_range=f"{mb(min(sizes))}～{mb(max(sizes))}", parts_zip_size=mb(os.path.getsize(pzip))))

    # 更早的上传文件夹只是硬链接，留着会把已被替换的旧版全本、旧分册一直占在磁盘上
    for d in os.listdir(os.path.dirname(out)):
        old = os.path.join(os.path.dirname(out), d)
        if d != os.path.basename(out) and d.startswith(f"{safe}-离线备份-") and os.path.isdir(old):
            shutil.rmtree(old)
            log(f"删除旧的上传文件夹 {d}/（zip 原件仍在 dist/ 里）")

    total = 0
    log(f"上传文件夹：{out}")
    for root, _, files in sorted(os.walk(out)):
        for fn in sorted(files):
            size = os.path.getsize(os.path.join(root, fn))
            total += size
            log(f"  {os.path.relpath(os.path.join(root, fn), out)}  {mb(size)}")
    log(f"共 {mb(total)}（硬链接，不额外占用磁盘）")
    return out
