"""本地存储：jsonl 追加写、状态文件原子写。"""
from __future__ import annotations

import json
import os


class TopicStore:
    def __init__(self, data_dir: str, topic_id: str):
        self.topic_id = topic_id
        self.root = os.path.join(data_dir, topic_id)
        self.pages_dir = os.path.join(self.root, "pages")
        self.images_dir = os.path.join(self.root, "images")
        self.html_dir = os.path.join(self.root, "html")
        self.replies_path = os.path.join(self.root, "replies.jsonl")
        self.updates_path = os.path.join(self.root, "updates.jsonl")
        self.images_log = os.path.join(self.root, "images.jsonl")
        self.topic_path = os.path.join(self.root, "topic.json")
        self.state_path = os.path.join(self.root, "state.json")
        for d in (self.pages_dir, self.images_dir, self.html_dir):
            os.makedirs(d, exist_ok=True)

    # ---- jsonl ----
    @staticmethod
    def read_jsonl(path: str):
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # 中断时可能留下半行，跳过即可（下次运行会重抓该页）
        return out

    @staticmethod
    def append_jsonl(path: str, records):
        if not records:
            return
        with open(path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # ---- json ----
    @staticmethod
    def read_json(path: str, default=None):
        if not os.path.exists(path):
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def write_json(path: str, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    # ---- 便捷方法 ----
    def replies(self):
        """按回复 ID 去重（保留最早抓到的一条），再按顺序叠加 updates.jsonl 里
        全量复查记下的变化（点赞数、楼主赞过、是否已删除）。"""
        seen = {}
        for r in self.read_jsonl(self.replies_path):
            seen.setdefault(r["id"], r)
        for u in self.read_jsonl(self.updates_path):
            r = seen.get(u.get("id"))
            if r is not None:
                r.update({k: v for k, v in u.items() if k not in ("id", "at")})
        return list(seen.values())

    def state(self):
        return self.read_json(self.state_path, {}) or {}

    def save_state(self, state):
        self.write_json(self.state_path, state)

    def save_page_html(self, start: int, html: str):
        with open(os.path.join(self.pages_dir, f"start_{start:06d}.html"), "w", encoding="utf-8") as f:
            f.write(html)
