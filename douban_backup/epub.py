"""手机版：每 500 楼一本 EPUB（分享版的内容，隐去用户 ID）。

和分享版的区别：阅读器大多不播视频，动图用缩小后的 GIF；楼层跳转改成目录（每 50 楼一章）；
不用脚本，样式只用阅读器普遍支持的写法。

目录：
  data/<帖子>/epub_media/   缩小后的 GIF 缓存，只转换新增的（静态图直接用分享版的 share_media/）
  dist/epub-<版本>/          生成的 .epub：全本/、分册-每N楼/ 和分册的 zip。
                            版本跟分享版一致（最近一次 share 的时间），这样同一次更新的电脑版和手机版对得上
"""
from __future__ import annotations

import html
import math
import os
import re
import subprocess
import time
import uuid
import zipfile

from lxml import etree
from lxml import html as lhtml

from .avatars import avatars_dir
from .crawl_images import done_map
from .render import FLOORS_PER_FILE, Renderer
from .share import adopt_dir, ffmpeg_exe, prepare_media
from .store import TopicStore

FLOORS_PER_CHAPTER = 50
GIF_WIDTH, GIF_FPS, GIF_COLORS = 400, 12, 128

CSS = """
body{margin:0 4px;font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;line-height:1.65}
h1{font-size:1.3em;line-height:1.4;margin:.6em 0}
h2{font-size:1em;color:#888;font-weight:normal;margin:1.2em 0 .4em;border-bottom:1px solid #ddd;padding-bottom:.2em}
a{color:#2f7a3a;text-decoration:none}
.r{margin:0 0 1.1em;padding:0 0 .9em;border-bottom:1px solid #e6e3dc;clear:both}
.h{font-size:.8em;color:#888;line-height:1.5;margin-bottom:.3em}
.h .av{width:2.2em;height:2.2em;border-radius:.3em;float:left;margin:0 .6em .2em 0}
.fl{font-weight:bold}
.u{color:#222;font-weight:bold}
.uid{color:#888;font-weight:normal}
.op{color:#2f7a3a;border:1px solid #9cc7a3;border-radius:.3em;padding:0 .25em;font-size:.9em}
.del{color:#a8452f;border:1px solid #e0a596;border-radius:.3em;padding:0 .25em;font-size:.9em}
.c{clear:both}
.c p{margin:.3em 0}
.c img,.pic img{max-width:100%;max-height:60vh;height:auto;display:block}
.pic{margin:.4em 0;clear:both}
.pic.sticker img{max-width:9em;max-height:9em}
blockquote.q{margin:.4em 0;padding:.3em .6em;border-left:3px solid #ccc;background:#f3f1ec;color:#777;font-size:.88em;clear:both;page-break-inside:avoid;break-inside:avoid;-webkit-column-break-inside:avoid}
blockquote.q img{max-width:9em;max-height:7em;display:block}
blockquote.q .qav{width:1.1em;height:1.1em;vertical-align:middle;margin-right:.2em}
.meta{color:#888;font-size:.85em}
.missing{color:#888;font-size:.85em}
.note{font-size:.9em;color:#555;border:1px solid #ddd;padding:.5em .8em;margin:1em 0}
"""


def xhtml_doc(title: str, body: str) -> str:
    """把 HTML 片段转成合法的 XHTML（豆瓣正文里有不闭合的标签和 HTML 实体）。"""
    wrapper = lhtml.fragment_fromstring(body, create_parent="div")
    inner = "".join(etree.tostring(el, method="xml", encoding="unicode") for el in wrapper)
    lead = html.escape(wrapper.text or "")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN" xml:lang="zh-CN">'
            f'<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>{html.escape(title)}</title>'
            '<link rel="stylesheet" type="text/css" href="../style.css"/></head>'
            f'<body>{lead}{inner}</body></html>')


def shrink_gif(src: str, dst: str) -> bool:
    vf = (f"fps={GIF_FPS},scale='min({GIF_WIDTH},iw)':-2:flags=lanczos,split[a][b];"
          f"[a]palettegen=max_colors={GIF_COLORS}:stats_mode=diff[p];"
          "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    tmp = dst + ".tmp.gif"
    ok = subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", src, "-vf", vf, "-loop", "0", tmp],
                        capture_output=True).returncode == 0
    if ok:
        os.replace(tmp, dst)
    elif os.path.exists(tmp):
        os.remove(tmp)
    return ok


def prepare_epub_media(store: TopicStore, pids, log=print):
    """pid -> (文件路径, 包内文件名)。动图转成小 GIF，其余用分享版压缩好的图片。"""
    share_index, share_dir = prepare_media(store, log)
    out_dir = os.path.join(store.root, "epub_media")
    os.makedirs(out_dir, exist_ok=True)
    done = done_map(store)
    out = {}
    for pid in pids:
        rec = done.get(pid)
        if not rec:
            continue
        if rec["format"] == "gif" or rec.get("video"):
            dst = os.path.join(out_dir, f"p{pid}.gif")
            if not os.path.exists(dst):
                src = os.path.join(store.images_dir, rec.get("video") or rec["file"])
                if not shrink_gif(src, dst):
                    log(f"  p{pid} 动图缩小失败，用原 GIF")
                    dst = os.path.join(store.images_dir, rec["file"])
            out[pid] = (dst, f"p{pid}.gif")
        else:
            name = (share_index.get(pid) or {}).get("img")
            if name:
                out[pid] = (os.path.join(share_dir, name), name)
    return out


class EpubRenderer(Renderer):
    """沿用分享版的楼层渲染（编号、去 ID、引用），只改链接和图片写法。"""

    def __init__(self, store, media):
        super().__init__(store, media=media, media_prefix="../media/", avatar_prefix="../media/avatars/", share=True)

    def content(self, s):
        # 正文里的换行是 <p> 里的 \n，网页版靠 pre-wrap 显示；阅读器不一定支持，换成 <br/>
        s = re.sub(r">\s*\n\s*<", "><", super().content(s))
        return s.replace("\n", "<br/>")

    def reply_link(self, ref_id, current_file):
        fl = self.floor_of.get(ref_id)
        if fl is None:
            return '<span class="meta">（原回复未备份）</span>'
        if self.volume_of(fl) != self.volume:
            return f'<span class="meta">#{fl}（第 {self.volume_of(fl) + 1} 册）</span>'
        return f'<a href="{chapter_file(fl)}#f{fl}">#{fl}</a>'

    def volume_of(self, floor):
        return 0 if floor == 0 else (floor - 1) // self.per_book

    def render_reply(self, r, floor, current_file):
        s = super().render_reply(r, floor, current_file)
        # 网页版用 flex 排头像；阅读器里改成头像浮动在标题行左边
        s = re.sub(r'^<article id="f(\d+)">(<img class="av"[^>]*>|<span class="av"></span>)<div class="b">(<a id="r[^"]*"></a>)<header>',
                   lambda m: f'<div class="r" id="f{m.group(1)}">{m.group(3)}<div class="h">'
                             + (m.group(2) if m.group(2).startswith("<img") else ""), s)
        s = s.replace("</header>", "</div>", 1)
        s = re.sub(r"<time>(.*?)</time><span>", r"<span> \1</span><span> ", s)
        s = s.replace('</a><span class="u">', '</a> <span class="u">', 1)
        s = s.replace('<span class="likes">', '<span> · ')
        s = re.sub(r"</div></article>$", "</div>", s)
        return s.replace(' loading="lazy"', "")

    def render_topic(self):
        s = super().render_topic()
        s = re.sub(r'^<article id="f0">(<img class="av"[^>]*>|<span class="av"></span>)<div class="b"><header>',
                   lambda m: '<div class="r" id="f0"><div class="h">' + (m.group(1) if m.group(1).startswith("<img") else ""), s)
        s = s.replace("</header>", "</div>", 1)
        s = re.sub(r"<time>(.*?)</time><span>", r"<span> \1</span><span> ", s)
        s = s.replace('</a><span class="u">', '</a> <span class="u">', 1)
        s = re.sub(r"</div></article>$", "</div>", s)
        return s.replace(' loading="lazy"', "")


def chapter_file(floor: int) -> str:
    k = 0 if floor == 0 else (floor - 1) // FLOORS_PER_CHAPTER
    return f"c{k:03d}.xhtml"


def make_cover(path: str, title: str, subtitle: str):
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1200, 1700
    im = Image.new("RGB", (W, H), (246, 243, 236))
    d = ImageDraw.Draw(im)
    font_path = next((p for p in ("/System/Library/Fonts/Hiragino Sans GB.ttc",
                                  "/System/Library/Fonts/STHeiti Medium.ttc") if os.path.exists(p)), None)
    big = ImageFont.truetype(font_path, 88) if font_path else ImageFont.load_default()
    small = ImageFont.truetype(font_path, 52) if font_path else ImageFont.load_default()
    d.rectangle([80, 80, W - 80, H - 80], outline=(47, 122, 58), width=6)
    lines, line = [], ""
    for ch in title:  # 按宽度折行
        if d.textlength(line + ch, font=big) > W - 300:
            lines.append(line)
            line = ""
        line += ch
    lines.append(line)
    y = 480
    for ln in lines:
        d.text((W // 2, y), ln, font=big, fill=(34, 34, 34), anchor="mm")
        y += 130
    d.text((W // 2, y + 120), subtitle, font=small, fill=(47, 122, 58), anchor="mm")
    d.text((W // 2, H - 200), "豆瓣小组帖子 · 离线纪念备份", font=small, fill=(136, 136, 136), anchor="mm")
    im.save(path, "JPEG", quality=85)


def build_volume(store: TopicStore, vol: int, dist_dir: str, per_book: int = FLOORS_PER_FILE, log=print) -> str:
    """生成第 vol 册（从 0 开始），每册 per_book 楼；per_book 不小于总楼数时就是全本。"""
    r0 = EpubRenderer(store, media={})
    n = len(r0.replies)
    full = per_book >= n
    lo, hi = vol * per_book + 1, min((vol + 1) * per_book, n)
    part = r0.replies[lo - 1:hi]
    topic = r0.topic if vol == 0 else None

    pids = []
    for r in part:
        for im in (r.get("image"), (r.get("quote") or {}).get("image")):
            if im and im["pid"] not in pids:
                pids.append(im["pid"])
    if topic:
        pids += [im["pid"] for im in topic.get("images", [])]
    files = prepare_epub_media(store, pids, log)

    r = EpubRenderer(store, media={pid: {"img": name} for pid, (_, name) in files.items()})
    r.volume, r.per_book = vol, per_book
    title = r.topic.get("title") or store.topic_id
    part_name = f"全本 · {lo}–{hi} 楼" if full else f"第 {vol + 1} 册 · {lo}–{hi} 楼"
    book_title = f"{title}（{part_name}）"

    items, spine, toc = [], [], []  # (id, href, media-type, props)
    chapters = []
    for k in range(math.ceil((hi - lo + 1) / FLOORS_PER_CHAPTER)):
        c_lo = lo + k * FLOORS_PER_CHAPTER
        c_hi = min(c_lo + FLOORS_PER_CHAPTER - 1, hi)
        body = ""
        if k == 0:
            last_time = r.replies[-1]["pubtime"] if r.replies else "-"
            body += (f"<h1>{html.escape(book_title)}</h1>"
                     # 分册的说明只写本册范围，不写全楼楼数：这样楼增高后，前面几册的内容完全不变
                     + (f"<div class='note'>本楼的离线纪念备份，收录到 {html.escape(last_time)}，全楼共 {n} 楼。" if full
                        else f"<div class='note'>本楼的离线纪念备份，每 {per_book} 楼一册，本册是第 {lo}–{hi} 楼。") +
                     "昵称后面的“·N号”是本备份给每位发帖人编的序号，用来区分同名用户，和豆瓣账号无关。"
                     "点引用里的楼层号可以跳过去。</div>")
            if topic:
                body += "<h2>主楼</h2>" + r.render_topic()
        body += f"<h2>第 {c_lo}–{c_hi} 楼</h2>"
        body += "".join(r.render_reply(rep, c_lo + i, None)
                        for i, rep in enumerate(r.replies[c_lo - 1:c_hi]))
        fname = chapter_file(c_lo)
        chapters.append((fname, xhtml_doc(f"第 {c_lo}–{c_hi} 楼", body)))
        toc.append((f"第 {c_lo}–{c_hi} 楼", f"text/{fname}"))

    used_avatars = set(re.findall(r'src="\.\./media/avatars/([^"]+)"', "".join(c for _, c in chapters)))

    os.makedirs(dist_dir, exist_ok=True)
    tag = "全本" if full else f"{vol + 1:02d}（{lo}-{hi}楼）"
    out = os.path.join(dist_dir, f"douban-{store.topic_id}-{tag}.epub")
    if not full:  # 最后一册的楼数会随更新变化，删掉同一册的旧文件，免得和新文件混在一起
        for f in os.listdir(dist_dir) if os.path.isdir(dist_dir) else []:
            if re.fullmatch(rf"douban-{store.topic_id}-{vol + 1:02d}（\d+-\d+楼）\.epub", f) and f != os.path.basename(out):
                os.remove(os.path.join(dist_dir, f))
                log(f"  删除旧文件 {f}")
    tmp = out + ".tmp"
    cover_path = os.path.join(store.root, "epub_media", f"cover-{'full' if full else f'{vol + 1:02d}'}.jpg")
    make_cover(cover_path, title, part_name)
    key = "full" if full else f"vol{vol}-{per_book}"
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'douban-{store.topic_id}-{key}')}"
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="utf-8"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
                   compress_type=zipfile.ZIP_DEFLATED)

        def add(arc, data=None, path=None, mtype=None, props=None, deflate=True):
            ctype = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
            if path:
                z.write(path, "OEBPS/" + arc, compress_type=ctype)
            else:
                z.writestr("OEBPS/" + arc, data, compress_type=ctype)
            iid = "i" + re.sub(r"[^A-Za-z0-9]", "_", arc)
            items.append((iid, arc, mtype, props))
            return iid

        add("style.css", CSS, mtype="text/css")
        add("cover.jpg", path=cover_path, mtype="image/jpeg", props="cover-image", deflate=False)
        cover_id = items[-1][0]
        spine.append(add("text/cover.xhtml", xhtml_doc(book_title, '<div style="text-align:center"><img src="../cover.jpg" alt="封面" style="max-width:100%;max-height:100%"/></div>'),
                         mtype="application/xhtml+xml"))
        for fname, doc in chapters:
            spine.append(add(f"text/{fname}", doc, mtype="application/xhtml+xml"))
        mtypes = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
        for pid, (path, name) in files.items():
            add(f"media/{name}", path=path, mtype=mtypes[name.rsplit(".", 1)[-1]], deflate=False)
        for f in sorted(used_avatars):
            add(f"media/avatars/{f}", path=os.path.join(avatars_dir(store), f), mtype=mtypes[f.rsplit(".", 1)[-1]], deflate=False)

        # 章节多时目录分两层：每 500 楼一组，组内每 50 楼一章
        per_group = FLOORS_PER_FILE // FLOORS_PER_CHAPTER
        groups = [toc[i:i + per_group] for i in range(0, len(toc), per_group)] if len(toc) > per_group else [[t] for t in toc]

        def group_label(g):
            first, last = g[0][0], g[-1][0]
            return first if len(g) == 1 else first.split("–")[0] + "–" + last.split("–")[1]

        nav = "".join(
            f'<li><a href="{g[0][1][5:]}">{html.escape(group_label(g))}</a>'
            + ("<ol>" + "".join(f'<li><a href="{h[5:]}">{html.escape(t)}</a></li>' for t, h in g) + "</ol>" if len(g) > 1 else "")
            + "</li>" for g in groups)
        add("text/nav.xhtml", '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN" xml:lang="zh-CN">'
            f'<head><meta charset="utf-8"/><title>目录</title></head><body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>{nav}</ol></nav></body></html>',
            mtype="application/xhtml+xml", props="nav")
        # 老一些的阅读器（含不少国内阅读器）只认 EPUB2 的 toc.ncx
        order = iter(range(1, 10 ** 6))

        def point(t, h, inner=""):
            k = next(order)
            return (f'<navPoint id="n{k}" playOrder="{k}"><navLabel><text>{html.escape(t)}</text></navLabel>'
                    f'<content src="{h}"/>{inner}</navPoint>')

        points = "".join(point(group_label(g), g[0][1], "".join(point(t, h) for t, h in g) if len(g) > 1 else "")
                         for g in groups)
        ncx_id = add("toc.ncx", '<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                     f'<head><meta name="dtb:uid" content="{book_id}"/></head><docTitle><text>{html.escape(book_title)}</text></docTitle>'
                     f'<navMap>{points}</navMap></ncx>', mtype="application/x-dtbncx+xml")

        manifest = "".join(f'<item id="{i}" href="{html.escape(h)}" media-type="{m}"' + (f' properties="{p}"' if p else "") + "/>"
                           for i, h, m, p in items)
        spine_xml = "".join(f'<itemref idref="{i}"/>' for i in spine)
        opf = ('<?xml version="1.0" encoding="utf-8"?>'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bid" xml:lang="zh-CN">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:identifier id="bid">{book_id}</dc:identifier><dc:title>{html.escape(book_title)}</dc:title>'
               '<dc:language>zh-CN</dc:language><dc:creator>豆瓣小组楼友</dc:creator>'
               f'<meta property="dcterms:modified">{modified}</meta><meta name="cover" content="{cover_id}"/></metadata>'
               f'<manifest>{manifest}</manifest><spine toc="{ncx_id}">{spine_xml}</spine></package>')
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
    os.replace(tmp, out)
    log(f"  {os.path.basename(out)}：{len(chapters)} 章，{len(files)} 张图（动图 {sum(1 for _, n in files.values() if n.endswith('.gif'))} 张），"
        f"{len(used_avatars)} 个头像，{os.path.getsize(out) / 1024 / 1024:.1f} MB")
    return out


def book_dir(dist_dir: str) -> str:
    return os.path.join(dist_dir, "全本")


def parts_dir(dist_dir: str, per_book: int) -> str:
    return os.path.join(dist_dir, f"分册-每{per_book}楼")


def parts_zip(store: TopicStore, dist_dir: str, per_book: int) -> str:
    return os.path.join(dist_dir, f"douban-{store.topic_id}-手机版-分册-每{per_book}楼.zip")


def zip_parts(store: TopicStore, dist_dir: str, per_book: int, log=print) -> str:
    """把分册文件夹打成一个 zip，方便一次下载全部分册。EPUB 本身已压缩，直接存。"""
    src = parts_dir(dist_dir, per_book)
    out = parts_zip(store, dist_dir, per_book)
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for f in sorted(os.listdir(src)):
            if f.endswith(".epub"):
                z.write(os.path.join(src, f), f"{os.path.basename(src)}/{f}")
    os.replace(tmp, out)
    log(f"  打包 {os.path.basename(out)}：{os.path.getsize(out) / 1024 / 1024:.0f} MB")
    return out


def release_version(store: TopicStore, log=print) -> str:
    """手机版沿用最近一次分享版的版本号；数据比分享版新时提醒先运行 share。"""
    rel = store.read_json(os.path.join(store.root, "releases", "latest.json")) or {}
    if not rel:
        log("  还没有分享版，版本号用当前时间")
        return time.strftime("%Y%m%d-%H%M")
    if rel.get("floors") != len(store.replies()):
        log(f"  ⚠️ 数据比分享版 {rel['version']} 新（{rel.get('floors')} → {len(store.replies())} 楼），"
            "建议先运行 share，手机版会跟着用新的版本号")
    return rel["version"]


def build(store: TopicStore, dist_dir: str = "dist", volumes=None, full=False,
          per_book: int = FLOORS_PER_FILE, log=print):
    """输出到 dist/epub-<版本>/：全本放在 全本/；分册放在 分册-每N楼/，并打一个 zip。
    per_book 按楼数固定分册（须是 500 的倍数）：楼增高后只有最后一册变化，满了再新开一册。"""
    n = len(store.replies())
    dist_dir = adopt_dir(dist_dir, "epub", release_version(store, log), log)
    if full:
        log(f"生成手机版 EPUB：全本 1 册，共 {n} 楼")
        return [build_volume(store, 0, book_dir(dist_dir), per_book=n, log=log)]
    if per_book % FLOORS_PER_FILE:
        raise SystemExit(f"--per-book 需要是 {FLOORS_PER_FILE} 的倍数")
    total = max(1, math.ceil(n / per_book))
    vols = [v - 1 for v in volumes] if volumes else range(total)
    log(f"生成手机版 EPUB：每册 {per_book} 楼，共 {total} 册，本次生成 {len(vols)} 册")
    out = [build_volume(store, v, parts_dir(dist_dir, per_book), per_book=per_book, log=log)
           for v in vols if 0 <= v < total]
    zip_parts(store, dist_dir, per_book, log)
    return out
