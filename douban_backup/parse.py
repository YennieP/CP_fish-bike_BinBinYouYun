"""把豆瓣小组帖子页 HTML 解析成结构化数据。纯函数，不发请求。"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

PID_RE = re.compile(r"/p(\d+)\.\w+")
VIEW_RE = re.compile(r"^https?://[^/]+(/view/[^/]+/)[^/]+(/public/p\d+\.\w+)")
PUBTIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*(.*)$")


def canonical_page_url(topic_id: str, start: int) -> str:
    return f"https://www.douban.com/group/topic/{topic_id}/?start={start}"


def start_of(url: str) -> int:
    qs = parse_qs(urlparse(url).query)
    return int(qs.get("start", ["0"])[0])


def raw_url(url: str):
    """把 /view/<类别>/<尺寸>/public/pXXX.jpg 换成 img3 域名上的 raw 尺寸（原文件，GIF 不会被转成静态图）。"""
    m = VIEW_RE.match(url or "")
    if not m:
        return None
    return "https://img3.doubanio.com" + m.group(1) + "raw" + m.group(2)


def _dedup(urls):
    out = []
    for u in urls:
        if u and u not in out:
            out.append(u)
    return out


def parse_image(img) -> dict | None:
    """回复 / 引用里的 <img>。候选地址按优先级排列：带签名的原图 → 无签名 raw → large。"""
    src = img.get("src") or ""
    photo = img.get("data-photo-url") or ""
    orig = img.get("data-original-url") or ""
    m = PID_RE.search(photo or src)
    if not m:
        return None
    return {
        "pid": m.group(1),
        "kind": img.get("data-type") or "normal",          # normal / sticker（表情包）
        "is_gif": img.get("data-render-type") == "gif",
        "sticker_id": img.get("data-sticker-id") or None,
        "candidates": _dedup([orig, raw_url(photo or src), photo, src]),
        "video_url": img.get("data-video-url") or None,
    }


def _text(el) -> str:
    return el.get_text(strip=True) if el else ""


def _avatar(el):
    img = el.select_one(".user-face img")
    return img.get("src") if img is not None else None


def parse_reply(li, page_start: int, page_index: int) -> dict:
    doc = li.select_one(".reply-doc")
    name_a = doc.select_one("h4 > a")
    pubtime = _text(doc.select_one("h4 .pubtime"))
    pm = PUBTIME_RE.match(pubtime)

    quote = None
    q = doc.select_one(".reply-quote .reply-quote-content")
    if q is not None:
        q_img = q.select_one(".quote-img img")
        q_author = q.select_one(".pubdate a")
        quote = {
            "ref_id": q.get("data-ref-cid"),
            "author_id": q.get("data-author-id"),
            "author_name": _text(q_author),
            "text": _text(q.select_one(".all.ref-content")) or _text(q.select_one(".short.ref-content")),
            "image": parse_image(q_img) if q_img else None,
        }

    # 回复自己的图片：直属于 .reply-doc 的 .comment-photos（引用里的在 .quote-img 下）
    image = None
    for photos in doc.find_all("div", class_="comment-photos", recursive=False):
        img = photos.select_one("img")
        if img is not None:
            image = parse_image(img)
            break

    body = doc.select_one(".reply-content .markdown") or doc.select_one(".reply-content")
    likes_m = re.search(r"\((\d+)\)", _text(doc.select_one("a.comment-vote")))

    return {
        "id": li.get("id") or li.get("data-cid"),
        "author_id": li.get("data-author-id"),
        "author_name": _text(name_a),
        "author_url": name_a.get("href") if name_a else None,
        "avatar_url": _avatar(li),
        "is_op": doc.select_one("h4 .topic-author-icon") is not None,
        "pubtime": pm.group(1) if pm else pubtime,
        "ip_location": pm.group(2) if pm else "",
        "content_html": body.decode_contents().strip() if body else "",
        "content_text": body.get_text("\n", strip=True) if body else "",
        "image": image,
        "quote": quote,
        "likes": int(likes_m.group(1)) if likes_m else 0,
        "op_liked": "楼主赞过" in _text(doc.select_one(".reaction-text")),
        "is_folded": li.get("data-is_folded") == "True",
        "page_start": page_start,
        "page_index": page_index,
    }


def parse_topic(soup, topic_id: str) -> dict | None:
    box = soup.select_one("#topic-content")
    if box is None:
        return None
    author_a = box.select_one(".topic-doc h3 .from a")
    body = box.select_one("#link-report .rich-content") or box.select_one("#link-report")
    images = []
    for img in body.select("img") if body else []:
        src = img.get("src") or ""
        m = PID_RE.search(src)
        if m:
            images.append({"pid": m.group(1), "kind": "topic", "is_gif": False, "sticker_id": None,
                           "candidates": _dedup([raw_url(src), src]), "video_url": None})
    h1 = soup.select_one("#content h1") or soup.select_one("h1")
    title = h1.find(string=True, recursive=False) if h1 else None
    return {
        "topic_id": topic_id,
        "title": (title or _text(h1)).strip(),
        "author_id": re.sub(r"\D", "", author_a.get("href", "")) if author_a else None,
        "author_name": _text(author_a),
        "avatar_url": _avatar(box),
        "create_time": _text(box.select_one(".create-time")),
        "edited": box.select_one(".update-time") is not None,
        "ip_location": _text(box.select_one(".ip-location")),
        "content_html": body.decode_contents().strip() if body else "",
        "images": images,
    }


def parse_page(html: str, topic_id: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    start = start_of(page_url)
    items = soup.select("#comments > li.comment-item")
    replies = [parse_reply(li, start, i) for i, li in enumerate(items)]

    next_a = soup.select_one(".paginator span.next a")
    next_url = canonical_page_url(topic_id, start_of(next_a["href"])) if next_a else None
    total = soup.select_one(".paginator .thispage")
    return {
        "start": start,
        "topic": parse_topic(soup, topic_id),
        "replies": replies,
        "popular_ids": [li.get("id") for li in soup.select("#popular-comments > li.comment-item")],
        "next_url": next_url,
        "total_pages": int(total["data-total-page"]) if total and total.get("data-total-page") else None,
    }
