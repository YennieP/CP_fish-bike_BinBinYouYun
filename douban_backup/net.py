"""网络层：单线程会话、随机限速、风控检测。

规则：遇到 403、验证码、跳转 sec.douban.com 或登录页，立刻抛出 Blocked，
由调用方保存进度后退出，不做任何重试。
"""
from __future__ import annotations

import os
import random
import re
import time

import requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
REFERER = "https://www.douban.com/"
DEFAULT_COOKIE_FILE = "~/.config/douban-backup/cookie.txt"

BLOCK_LOCATION_HINTS = ("sec.douban.com", "accounts.douban.com", "passport", "login")
BLOCK_TITLE_HINTS = ("没有访问权限", "禁止访问", "登录豆瓣", "验证", "异常请求")


class Blocked(Exception):
    """被风控 / 需要登录 / 需要验证。必须停下来交给用户处理。"""


def load_cookie() -> str:
    cookie = os.environ.get("DOUBAN_COOKIE", "").strip()
    if cookie:
        return cookie
    path = os.path.expanduser(os.environ.get("DOUBAN_COOKIE_FILE", DEFAULT_COOKIE_FILE))
    if not os.path.exists(path):
        raise SystemExit(
            f"找不到 Cookie：请设置环境变量 DOUBAN_COOKIE，或把 Cookie 存到 {DEFAULT_COOKIE_FILE}"
        )
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class Client:
    def __init__(self, page_delay=(3.0, 6.0), image_delay=(1.0, 2.5)):
        self.page_delay = page_delay
        self.image_delay = image_delay
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._cookie = None  # 只在请求豆瓣页面时带上，不发给图片 CDN

    def _wait(self, delay_range):
        lo, hi = delay_range
        target = self._last_request + random.uniform(lo, hi)
        now = time.time()
        if target > now:
            time.sleep(target - now)
        self._last_request = time.time()

    def _get(self, url, **kw):
        """网络层错误（断网、超时）不是风控信号：等 15 秒、45 秒各重试一次，仍失败再抛出。
        HTTP 层面的 403 / 跳转不在这里重试，由调用方当作 Blocked 处理。"""
        for backoff in (15, 45, None):
            try:
                return self.session.get(url, allow_redirects=False, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                if backoff is None:
                    raise
                print(f"    （网络错误 {type(e).__name__}，{backoff} 秒后重试）", flush=True)
                time.sleep(backoff)

    def get_page(self, url: str) -> str:
        if self._cookie is None:
            self._cookie = load_cookie()
        self._wait(self.page_delay)
        resp = self._get(url, headers={"Cookie": self._cookie}, timeout=30)
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            raise Blocked(f"页面被重定向到 {loc or '(未知)'}，可能需要登录或验证")
        if resp.status_code != 200:
            raise Blocked(f"页面返回 HTTP {resp.status_code}")
        resp.encoding = "utf-8"
        html = resp.text
        # 正常帖子页一定有 #topic-content 或 #comments；只有缺失时才看标题，
        # 避免帖子标题里恰好含“验证”之类的词被误判
        if 'id="comments"' in html or 'id="topic-content"' in html:
            return html
        m = re.search(r"<title>\s*([^<]*?)\s*</title>", html)
        title = m.group(1) if m else ""
        hint = "，可能需要登录或验证" if any(h in title for h in BLOCK_TITLE_HINTS) else ""
        raise Blocked(f"页面里找不到帖子内容（标题“{title}”）{hint}")

    def get_image(self, url: str):
        """返回 (status_code, bytes)。3xx 跳到风控域名时抛 Blocked。"""
        self._wait(self.image_delay)
        resp = self._get(url, timeout=60)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if any(h in loc for h in BLOCK_LOCATION_HINTS):
                raise Blocked(f"图片请求被重定向到 {loc}")
            return resp.status_code, b""
        return resp.status_code, resp.content
