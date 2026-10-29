"""整理上传网盘用的文件夹：电脑版完整包（和更新包）、手机版全本、手机版分册 zip，外加一份下载说明。

文件用硬链接放进去，不额外占空间。打包前检查这些文件是不是用最新的数据生成的。

  dist/upload/<上传文件夹>/
    00-先看我-下载说明.txt
    电脑版/              电脑版完整包（有更新包时一并放入）
    手机版-全本.epub
    手机版-分N册.zip      全部分册打成一个包（解压后是 N 个 EPUB）

文件名可以在 data/<帖子>/bundle.json 里改（不进 Git），可用 {title} {date} {version} {prev} {n} 占位，例如：
  {"folder": "鱼车の天后豆瓣楼-{date}", "computer_full": "鱼车天后楼电脑版-full-{version}.zip",
   "computer_update": "鱼车天后楼电脑版-update-{prev}-to-{version}.zip"}
"""
from __future__ import annotations

import os
import re
import shutil
import zipfile

from . import epub
from .store import TopicStore

NAMES = {
    "folder": "{title}-离线备份-{version}",
    "computer_full": "douban-{tid}-full-{version}.zip",
    "computer_update": "douban-{tid}-update-{prev}-to-{version}.zip",
    "book": "手机版-全本.epub",
    "parts_zip": "手机版-分{n}册.zip",
}

GUIDE = """\
{title} · 离线备份
================================================

版本：{version}
收录到 {last_time}（北京时间），共 {floors} 楼（不含主楼）。

按自己的设备挑一种下载就行，不用全下。看的时候都不需要联网，也不需要装软件。

  在电脑上看 → 电脑版/ 里的 {full_name}（{full_size}）
  在手机上看 → {book_name}（{book_size}）或 {parts_name}（{parts_zip_size}），二选一


一、在电脑上看
--------------
1. 下载 电脑版/ 里的 {full_name}。
2. 解压，会得到一个 {folder} 文件夹。
3. 打开这个文件夹，双击里面的“点我打开.html”，就会用浏览器打开目录。
   每页顶部可以输入楼层号直接跳转；动图会在滚动到时自动播放。
{update_note}

二、在手机上看
--------------
手机版是 EPUB 电子书，有两种，挑一种：

  {book_name}（{book_size}）：整楼一本
    · 引用里的楼层都能点过去。翻页流畅，跳到很远的楼层时会卡一下。
    · 以后每次更新，都要整本重新下载。

  {parts_name}（{parts_zip_size}）：分成 {n_parts} 本，打成了一个压缩包
    · 每 {per_book} 楼一本（{part_list}），每本 {part_range}。
    · 文件小，跳转快；引用到别的册子的楼层只显示楼层号，点不过去。
    · 以后更新时，要重新下载这个压缩包，但前面的册子内容不会变，只需要把最后一两本重新导入阅读器。{changed_note}

怎么打开（以 iPhone 为例；网盘 App 的按钮名称可能略有不同）：
  整楼一本：
    1. 在网盘 App 里选中 {book_name}，点“下载”，存到手机上。
       不要直接点开预览，网盘里预览多半显示不全。
    2. 下载完成后，点“用其他应用打开”（或“分享”），选“图书”（Apple Books）。
  分册压缩包：
    1. 在网盘 App 里下载 {parts_name}，选择存到“文件”App。
    2. 在“文件”App 里点一下这个 zip，会自动解压出一个文件夹，里面是 {n_parts} 个 EPUB。
    3. 点开其中一本，点“分享”，选“图书”。每一本都这样导入一次。
  之后在“图书”的书库里就能看到。
  已在 iPhone 的“图书”上试过。安卓手机可以用文件管理器解压，再用微信读书、多看、静读天下等阅读器打开，效果可能不同。

更新：手机版没有更新包。有新版本时，先在阅读器里删掉旧的那本，再导入新的。


小提示
------
  · 昵称后面的“·N号”是本备份给每位发帖人编的序号，用来区分同名的人，和豆瓣账号无关。
  · 标着“原帖已删除”的回复，在豆瓣上已经被删了，这里保留了原文。
  · 备份内容的版权属于原发帖人，只在本楼内分享，请不要公开转载。
"""


def mb(n: int) -> str:
    return f"{n / 1024 / 1024 / 1024:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 / 1024:.0f} MB"


def place(src: str, dst_dir: str, name: str | None = None):
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name or os.path.basename(src))
    try:
        os.link(src, dst)  # 同一块磁盘上不占额外空间
    except OSError:
        shutil.copyfile(src, dst)


def names(store: TopicStore) -> dict:
    custom = store.read_json(os.path.join(store.root, "bundle.json"), {}) or {}
    return {**NAMES, **{k: v for k, v in custom.items() if k in NAMES}}


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
    nm = names(store)
    fill = dict(title=safe, tid=tid, version=version, date=version[:8], n=len(parts))
    out = os.path.join(dist_dir, "upload", nm["folder"].format(**fill))
    if os.path.exists(out):
        shutil.rmtree(out)  # 只含硬链接和说明，删掉不影响 dist 里的原文件
    full_name = nm["computer_full"].format(**fill)
    place(full_zip, os.path.join(out, "电脑版"), full_name)
    update_note = changed_note = ""
    if update_zips:
        u = update_zips[-1]
        prev = re.search(r"-update-(\d{8}-\d{4})-to-", u).group(1)
        u_name = nm["computer_update"].format(prev=prev, **fill)
        place(os.path.join(dist_dir, u), os.path.join(out, "电脑版"), u_name)
        update_note = (f"\n更新：装过上一版（{prev}）的 Windows 用户，可以只下载 电脑版/ 里的更新包\n"
                       f"   {u_name}（{mb(os.path.getsize(os.path.join(dist_dir, u)))}），只含有变化的文件。\n"
                       "   详细步骤见电脑版文件夹里的“使用说明.txt”。Mac 用户、或者漏过版本的人，请重新下载完整包。\n")
        # 按上一版的楼数算出这次变了哪几册（和 update 重新生成分册的规则一致）
        prev_floors = (store.read_json(os.path.join(store.root, "releases", f"{prev}.json")) or {}).get("floors")
        if prev_floors:
            first = max(1, -(-prev_floors // per_book))
            if first > 1:
                changed_note = (f"\n      这次（相比 {prev}）变化的是第 {first}" + (f"–{len(parts)}" if len(parts) > first else "")
                                + " 本，前面的不用重新导入。")
    book_name, parts_name = nm["book"].format(**fill), nm["parts_zip"].format(**fill)
    place(book, out, book_name)
    place(pzip, out, parts_name)

    sizes = [os.path.getsize(p) for p in parts]
    part_list = "、".join(f"第 {i} 本 {a}–{b} 楼" for i, (a, b) in enumerate(spans, 1))
    with open(os.path.join(out, "00-先看我-下载说明.txt"), "w", encoding="utf-8") as f:
        f.write(GUIDE.format(title=title, version=version, last_time=release["last_time"], floors=floors,
                             folder=f"douban-{tid}", full_name=full_name,
                             full_size=mb(os.path.getsize(full_zip)), update_note=update_note,
                             book_name=book_name, book_size=mb(os.path.getsize(book)),
                             parts_name=parts_name, n_parts=len(parts), per_book=per_book, part_list=part_list,
                             changed_note=changed_note, part_range=f"{mb(min(sizes))}～{mb(max(sizes))}",
                             parts_zip_size=mb(os.path.getsize(pzip))))

    # 更早的上传文件夹只是硬链接，留着会把已被替换的旧版全本、旧分册一直占在磁盘上
    for d in os.listdir(os.path.dirname(out)):
        old = os.path.join(os.path.dirname(out), d)
        if d != os.path.basename(out) and os.path.isdir(old):
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
