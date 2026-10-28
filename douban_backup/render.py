"""由 replies.jsonl + topic.json + 图片生成离线 HTML。

两种模式：
- 存档版（archive）：输出到 data/<帖子>/html/，引用原始图片（GIF 原样）。
- 分享版（share）：输出到分享包目录，引用压缩后的素材（GIF 已转 MP4），
  用户只显示昵称 + 本帖内的编号，不出现用户 ID 和主页链接。

楼层号：按回复 ID 升序编号（主楼是第 0 楼），每 FLOORS_PER_FILE 楼一个文件。
每层楼有两个锚点：#f<楼层号> 和 #r<回复ID>。
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import time

from .crawl_images import done_map
from .store import TopicStore

FLOORS_PER_FILE = 500

CSS = """
:root{--bg:#fbfaf7;--fg:#222;--muted:#888;--line:#e6e3dc;--card:#fff;--accent:#2f7a3a;--quote:#f3f1ec;--badge:#e8f3ea}
@media (prefers-color-scheme:dark){:root{--bg:#17181a;--fg:#ddd;--muted:#8b8d91;--line:#2c2e31;--card:#1f2023;--accent:#7cc487;--quote:#26282b;--badge:#23352a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif}
main{max-width:760px;margin:0 auto;padding:16px}
a{color:var(--accent);text-decoration:none}
nav.bar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:14px}
nav.bar form{margin-left:auto;display:flex;gap:6px}
nav.bar input{width:7em;padding:3px 6px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
h1{font-size:22px;line-height:1.4}
article{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0;scroll-margin-top:56px;display:flex;gap:12px;align-items:flex-start}
article>.b{flex:1;min-width:0}
.av{flex:none;width:40px;height:40px;border-radius:6px;object-fit:cover;background:var(--quote);display:block}
.qav{width:16px;height:16px;border-radius:3px;vertical-align:-3px;margin-right:3px;object-fit:cover}
article:target{outline:2px solid var(--accent)}
article header{font-size:13px;color:var(--muted);display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
article header .fl{font-weight:600}
article header .u{color:var(--fg);font-weight:600}
.uid{color:var(--muted);font-weight:400}
.op{background:var(--badge);color:var(--accent);border-radius:4px;padding:0 5px;font-size:12px}
.likes{margin-left:auto}
.del{background:#f6e3df;color:#a8452f;border-radius:4px;padding:0 5px;font-size:12px}
@media (prefers-color-scheme:dark){.del{background:#3a2522;color:#e39a87}}
.c p{margin:.4em 0;white-space:pre-wrap;word-break:break-word}
.c img,.pic img,.pic video{max-width:100%;max-height:420px;width:auto;height:auto;border-radius:6px;display:block}
.pic.sticker img,.pic.sticker video{max-width:200px;max-height:200px}
.pic{margin:8px 0}
blockquote.q{margin:8px 0;padding:6px 10px;background:var(--quote);border-left:3px solid var(--line);border-radius:4px;font-size:14px;color:var(--muted)}
blockquote.q img,blockquote.q video{max-height:120px;max-width:200px;border-radius:4px;display:block;margin-bottom:4px}
.missing{display:inline-block;padding:6px 10px;border:1px dashed var(--line);border-radius:6px;font-size:13px;color:var(--muted)}
.pager{display:flex;justify-content:space-between;margin:24px 0;font-size:15px}
ul.files{list-style:none;padding:0}
ul.files li{padding:8px 0;border-bottom:1px solid var(--line)}
.meta{color:var(--muted);font-size:13px}
.note{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-size:14px}
"""

JS = """
<script>
function jump(e){e.preventDefault();var n=parseInt(document.getElementById('jf').value,10);if(isNaN(n)||n<0)return false;
var per=%d,k=n===0?0:Math.floor((n-1)/per),lo=k*per+1,hi=(k+1)*per;
var pad=function(x){return String(x).padStart(5,'0')};location.href=(window.PFX||'')+pad(lo)+'-'+pad(hi)+'.html#f'+n;return false}
// 上方图片陆续加载会把目标楼层挤下去：加载期间持续把锚点拉回视野
(function(){var h=location.hash;if(!h)return;var t0=Date.now();function fix(){var el=document.getElementById(h.slice(1));if(el)el.scrollIntoView();}
var iv=setInterval(function(){fix();if(Date.now()-t0>3000)clearInterval(iv)},300);
['wheel','touchstart','keydown','mousedown'].forEach(function(ev){addEventListener(ev,function(){clearInterval(iv)},{once:true,passive:true})});
addEventListener('hashchange',function(){h=location.hash;fix()});})();
// 动图视频：滚到可见时才加载并播放，离开视野就暂停，一页几十个也不卡
addEventListener('DOMContentLoaded',function(){var vs=document.querySelectorAll('video[data-gif]');
if(!('IntersectionObserver' in window)){vs.forEach(function(v){v.preload='auto';v.play()});return}
var io=new IntersectionObserver(function(es){es.forEach(function(e){var v=e.target;if(e.isIntersecting){v.play().catch(function(){})}else{v.pause()}})},{rootMargin:'200px'});
vs.forEach(function(v){io.observe(v)})});
</script>
""" % FLOORS_PER_FILE

DEFAULT_NAME_RE = re.compile(r"^(豆友|用户)\d{5,}$")
PEOPLE_LINK_RE =re.compile(r'(?is)<a\b[^>]*href="https?://(?:www\.|m\.)?douban\.com/people/[^"]*"[^>]*>(.*?)</a>')


def file_for_floor(floor: int) -> str:
    k = 0 if floor == 0 else (floor - 1) // FLOORS_PER_FILE
    return f"{k * FLOORS_PER_FILE + 1:05d}-{(k + 1) * FLOORS_PER_FILE:05d}.html"


def esc(s) -> str:
    return html.escape(str(s or ""))


def clean_html(s: str) -> str:
    return re.sub(r"(?is)<script.*?</script>", "", s or "")


def avatar_files(store: TopicStore):
    from .avatars import files_by_user
    return files_by_user(store)


def archive_media(store: TopicStore):
    """存档版素材：pid -> {"img": 文件名, "video": 文件名}（文件都在 images/ 下）。"""
    out = {}
    for pid, rec in done_map(store).items():
        out[pid] = {"img": rec["file"], "video": rec.get("video")}
    return out


class Renderer:
    def __init__(self, store: TopicStore, out_dir: str | None = None, media=None,
                 media_prefix: str = "../images/", avatar_prefix: str = "../avatars/", share: bool = False,
                 entry: str = "index.html", pages_dir: str = ""):
        """entry 是目录页文件名；pages_dir 非空时，分卷放进这个子文件夹，目录页留在最外层
        （分享版用它让朋友一打开文件夹就看到入口）。media_prefix 是相对分卷所在位置的路径。"""
        self.store = store
        self.out_dir = out_dir or store.html_dir
        self.media = archive_media(store) if media is None else media
        self.media_prefix = media_prefix
        self.avatars = avatar_files(store)  # 用户ID -> 头像文件名（按内容哈希命名，不含 ID）
        self.avatar_prefix = avatar_prefix
        self.share = share
        self.entry, self.pages_dir = entry, pages_dir
        self.topic = store.read_json(store.topic_path) or {}
        self.replies = sorted(store.replies(), key=lambda r: int(r["id"]))
        self.floor_of = {r["id"]: i for i, r in enumerate(self.replies, 1)}
        self.user_no = self._number_users() if share else {}

    def _number_users(self):
        """分享版的用户编号：按首次出现顺序编 1、2、3…。已备份的回复不会消失，
        新回复只会追加新编号，所以每次更新后老用户的编号都不变。"""
        nums = {}
        order = [self.topic.get("author_id")]
        for r in self.replies:
            order.append(r.get("author_id"))
            order.append((r.get("quote") or {}).get("author_id"))
        for uid in order:
            if uid and uid not in nums:
                nums[uid] = len(nums) + 1
        return nums

    def user(self, name, uid):
        if self.share:
            # 没设昵称的用户，豆瓣显示为“豆友<用户ID>”，分享版里把 ID 去掉
            name = DEFAULT_NAME_RE.sub(r"\1", name or "")
            tail = f'<span class="uid">·{self.user_no[uid]}号</span>' if uid in self.user_no else ""
        else:
            tail = f'<span class="uid">·{esc(uid[-4:])}</span>' if uid else ""
        return f'<span class="u">{esc(name)}{tail}</span>'

    def avatar(self, uid, cls="av"):
        f = self.avatars.get(uid)
        if not f:
            return '<span class="av"></span>' if cls == "av" else ""
        return f'<img class="{cls}" src="{self.avatar_prefix}{esc(f)}" loading="lazy" alt="">'

    def content(self, s):
        s = clean_html(s)
        if self.share:  # 不保留用户主页链接，只留文字
            s = PEOPLE_LINK_RE.sub(r"<span>\1</span>", s)
        return s

    def img(self, image, cls="pic"):
        if not image:
            return ""
        m = self.media.get(image["pid"]) or {}
        p = self.media_prefix
        if m.get("video"):  # 动图：静音循环播放，效果同 GIF
            poster = f' poster="{p}{esc(m["img"])}"' if m.get("img") else ""
            tag = f'<video data-gif src="{p}{esc(m["video"])}"{poster} loop muted playsinline preload="none"></video>'
        elif m.get("img"):
            tag = f'<img src="{p}{esc(m["img"])}" loading="lazy" alt="">'
        else:
            tag = f'<span class="missing">图片 p{esc(image["pid"])} 未下载</span>'
        if cls is None:
            return tag
        kind = " sticker" if image.get("kind") == "sticker" else ""
        return f'<div class="{cls}{kind}">{tag}</div>'

    def reply_link(self, ref_id, current_file):
        fl = self.floor_of.get(ref_id)
        if fl is None:
            return '<span class="meta">（原回复未备份）</span>'
        f = file_for_floor(fl)
        href = f"#f{fl}" if f == current_file else f"{f}#f{fl}"
        return f'<a href="{href}">#{fl}</a>'

    def render_reply(self, r, floor, current_file):
        q = r.get("quote")
        quote = ""
        if q:
            quote = (
                '<blockquote class="q">'
                + self.img(q.get("image"), cls=None)
                + f'<div>{esc(q.get("text"))}</div>'
                + f'<div class="meta">— {self.avatar(q.get("author_id"), "qav")}{self.user(q.get("author_name"), q.get("author_id"))} {self.reply_link(q.get("ref_id"), current_file)}</div>'
                + "</blockquote>"
            )
        badges = ' <span class="op">楼主</span>' if r.get("is_op") else ""
        if r.get("op_liked"):
            badges += ' <span class="op">楼主赞过</span>'
        if r.get("deleted"):
            badges += ' <span class="del">原帖已删除</span>'
        return (
            f'<article id="f{floor}">{self.avatar(r.get("author_id"))}<div class="b"><a id="r{esc(r["id"])}"></a><header>'
            f'<a class="fl" href="#f{floor}">#{floor}</a>{self.user(r.get("author_name"), r.get("author_id"))}{badges}'
            f'<time>{esc(r.get("pubtime"))}</time><span>{esc(r.get("ip_location"))}</span>'
            f'<span class="likes">赞 {r.get("likes", 0)}</span></header>'
            f'{quote}{self.img(r.get("image"))}<div class="c">{self.content(r.get("content_html"))}</div></div></article>'
        )

    def render_topic(self):
        t = self.topic
        if not t:
            return ""
        body = self.content(t.get("content_html"))
        for img in t.get("images", []):  # 主楼图片换成本地路径
            f = (self.media.get(img["pid"]) or {}).get("img")
            if f:
                body = re.sub(r'src="[^"]*/p%s\.\w+[^"]*"' % img["pid"],
                              f'src="{self.media_prefix}{f}" loading="lazy"', body)
        return (
            f'<article id="f0">{self.avatar(t.get("author_id"))}<div class="b"><header><a class="fl" href="#f0">#0 主楼</a>'
            f'{self.user(t.get("author_name"), t.get("author_id"))} <span class="op">楼主</span>'
            f'<time>{esc(t.get("create_time"))}</time><span>{esc(t.get("ip_location"))}</span></header>'
            f'<div class="c">{body}</div></div></article>'
        )

    def page(self, title, inner, pfx=""):
        # pfx：从这一页到分卷所在文件夹的相对路径，给楼层跳转用
        pfx_js = f"<script>var PFX={json.dumps(pfx)}</script>" if pfx else ""
        return (
            f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><style>{CSS}</style>{pfx_js}{JS}</head><body>{inner}</body></html>'
        )

    def nav(self, prev_f=None, next_f=None, at_entry=False):
        home = self.entry if at_entry or not self.pages_dir else f"../{self.entry}"
        links = [f'<a href="{home}">目录</a>']
        if prev_f:
            links.append(f'<a href="{prev_f}">‹ 上一份</a>')
        if next_f:
            links.append(f'<a href="{next_f}">下一份 ›</a>')
        form = '<form onsubmit="return jump(event)"><input id="jf" inputmode="numeric" placeholder="跳到楼层"><button>跳转</button></form>'
        return f'<nav class="bar">{" ".join(links)}{form}</nav>'

    def run(self, log=print):
        out = self.out_dir
        pages_out = os.path.join(out, self.pages_dir) if self.pages_dir else out
        os.makedirs(pages_out, exist_ok=True)
        sub = f"{self.pages_dir}/" if self.pages_dir else ""
        n = len(self.replies)
        chunks = max(1, math.ceil(n / FLOORS_PER_FILE))
        files = [file_for_floor(k * FLOORS_PER_FILE + 1) for k in range(chunks)]
        title = self.topic.get("title") or self.store.topic_id
        listing = []
        for k, fname in enumerate(files):
            lo, hi = k * FLOORS_PER_FILE + 1, min((k + 1) * FLOORS_PER_FILE, n)
            part = self.replies[lo - 1:hi]
            body = [self.render_topic()] if k == 0 else []
            body += [self.render_reply(r, lo + i, fname) for i, r in enumerate(part)]
            prev_f = files[k - 1] if k > 0 else None
            next_f = files[k + 1] if k + 1 < chunks else None
            pager = '<div class="pager">' + (f'<a href="{prev_f}">‹ 上一份</a>' if prev_f else "<span></span>") + (f'<a href="{next_f}">下一份 ›</a>' if next_f else "<span></span>") + "</div>"
            inner = self.nav(prev_f, next_f) + f"<main><h1>{esc(title)}</h1><p class='meta'>第 {lo}–{hi} 楼</p>" + "".join(body) + pager + "</main>"
            with open(os.path.join(pages_out, fname), "w", encoding="utf-8") as f:
                f.write(self.page(f"{title} · {lo}-{hi} 楼", inner))
            span = f"{part[0]['pubtime']} ~ {part[-1]['pubtime']}" if part else ""
            listing.append(f'<li><a href="{sub}{fname}">第 {lo}–{hi} 楼</a> <span class="meta">{esc(span)}</span></li>')

        last_time = self.replies[-1]["pubtime"] if self.replies else "-"
        if self.share:
            intro = (f"<div class='note'>这是本楼的离线纪念备份，收录到 {esc(last_time)}，共 {n} 楼（不含主楼）。"
                     f"不需要联网，动图会在滚动到时自动播放。<br>"
                     f"昵称后面的“·N号”是本备份里给每位发帖人编的序号，用来区分同名用户，和豆瓣账号无关。<br>"
                     f"点下面的分卷进入；也可以在右上角输入楼层号直接跳转。更新方法见同一文件夹里的“使用说明.txt”。</div>")
        else:
            total_pages = self.store.state().get("total_pages", "?")
            summary = f"共 {n} 条回复（不含主楼），收录到 {last_time}。原帖共 {total_pages} 页。"
            intro = (f"<p class='meta'>{esc(summary)}</p>"
                     f"<p class='meta'>楼层号按回复 ID 顺序编号，可能和豆瓣 App 上的页码对不上。</p>")
        inner = self.nav(at_entry=True) + (f"<main><h1>{esc(title)}</h1>{intro}"
                              f"<p class='meta'>生成于 {time.strftime('%Y-%m-%d %H:%M')}</p>"
                              f"<ul class='files'>{''.join(listing)}</ul></main>")
        with open(os.path.join(out, self.entry), "w", encoding="utf-8") as f:
            f.write(self.page(title, inner, pfx=sub))
        log(f"已生成 {chunks} 个分卷 + {self.entry} → {out}")
        return [sub + f for f in files] + [self.entry]
