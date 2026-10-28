#!/usr/bin/env python3
"""豆瓣小组长帖备份。

  python backup.py update                    # 一键增量更新：补数据 → 生成全部成品 → 整理上传文件夹
  python backup.py text   --max-pages 5      # 阶段一：抓文字（从断点继续）
  python backup.py text   --refresh          # 全量复查：更新点赞、主楼，标记已删除的回复
  python backup.py images --max 200          # 阶段二：下载图片（从断点继续）
  python backup.py avatars                   # 阶段二之二：下载头像（从断点继续）
  python backup.py render                    # 阶段三：生成 HTML
  python backup.py share                     # 生成分享版并打包（完整包 + 更新包）
  python backup.py epub --volumes 1          # 手机版：每 500 楼一本 EPUB
  python backup.py bundle                    # 整理上传网盘用的文件夹（电脑版 + 手机版 + 下载说明）
  python backup.py status                    # 查看进度
"""
from __future__ import annotations

import argparse
import sys
import warnings

# macOS 自带 Python 3.9 链接的是 LibreSSL，urllib3 会每次警告一遍；不影响使用
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests

from douban_backup import crawl_images, crawl_text
from douban_backup.net import Blocked, Client
from douban_backup.render import Renderer
from douban_backup.store import TopicStore

DEFAULT_TOPIC = "500345559"


def blocked_exit(e: Blocked):
    print(
        f"\n⛔ 已暂停：{e}\n"
        "已抓到的内容都已保存，下次运行会从断点继续。\n"
        "请先用浏览器打开豆瓣确认账号状态（是否要求验证或重新登录），"
        "必要时更新 ~/.config/douban-backup/cookie.txt，隔一段时间再运行。",
        file=sys.stderr,
    )
    sys.exit(2)


def main():
    p = argparse.ArgumentParser(description="豆瓣小组长帖备份")
    p.add_argument("--topic", default=DEFAULT_TOPIC, help="帖子 ID（默认 %(default)s）")
    p.add_argument("--data-dir", default="data")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("text", help="抓文字，每页追加写 replies.jsonl")
    t.add_argument("--max-pages", type=int, help="本次最多抓几页（默认 5；--refresh 时默认不限）")
    t.add_argument("--start", type=int, help="不用断点，从指定 start 值开始（如 0、100、200）")
    t.add_argument("--refresh", action="store_true",
                   help="全量复查：从第 1 页翻到末页，更新点赞数和主楼，把没翻到的回复标记为已删除（可中断续查）")

    i = sub.add_parser("images", help="下载图片")
    i.add_argument("--max", type=int, help="本次最多下载几张")

    a = sub.add_parser("avatars", help="下载发帖人头像（按内容命名，不含用户 ID）")
    a.add_argument("--max", type=int, help="本次最多下载几个")

    sub.add_parser("render", help="生成 HTML")
    sub.add_parser("status", help="查看进度")
    sub.add_parser("progress", help="实时进度条（只读，不发请求）")
    sh = sub.add_parser("share", help="生成分享版（GIF 转视频、图片压缩、隐去用户 ID）并打包")
    sh.add_argument("--no-zip", action="store_true", help="只生成文件夹，不打包、不记录版本")
    sh.add_argument("--dist", default="dist", help="输出目录（默认 dist）")
    ep = sub.add_parser("epub", help="生成手机版 EPUB（每 500 楼一册，动图用缩小的 GIF）")
    ep.add_argument("--volumes", type=int, nargs="+", help="只生成这几册（从 1 开始），默认全部")
    ep.add_argument("--dist", default="dist", help="输出到这里的 epub-<版本>/（默认 dist）")
    ep.add_argument("--full", action="store_true", help="整楼合成一本（文件很大，先在手机上试能否打开）")
    ep.add_argument("--per-book", type=int, default=500, help="每册多少楼（500 的倍数，默认 500）")
    sub.add_parser("bundle", help="整理上传网盘用的文件夹（硬链接，不额外占空间）")
    up = sub.add_parser("update", help="一键增量更新：补新回复、图片、头像，生成存档版、分享版、手机版，整理上传文件夹")
    up.add_argument("--refresh", action="store_true", help="用全量复查代替补新回复（更新点赞、主楼、删帖标记），分册全部重新生成")
    up.add_argument("--offline", action="store_true", help="不联网，只用已有数据重新生成成品")
    up.add_argument("--per-book", type=int, default=2500, help="手机版每册多少楼（默认 2500）")

    args = p.parse_args()
    store = TopicStore(args.data_dir, args.topic)

    try:
        if args.cmd == "text":
            if args.refresh:
                if args.start is not None:
                    p.error("--refresh 总是从第 1 页（或上次复查的断点）开始，不能和 --start 一起用")
                crawl_text.refresh(Client(), store, args.max_pages or 10000)
            else:
                crawl_text.run(Client(), store, args.max_pages or 5, start=args.start)
        elif args.cmd == "images":
            crawl_images.run(Client(), store, args.max)
        elif args.cmd == "avatars":
            from douban_backup import avatars
            avatars.run(Client(), store, args.max)
        elif args.cmd == "render":
            Renderer(store).run()
        elif args.cmd == "share":
            from douban_backup import share
            share.build(store, args.dist, make_zip=not args.no_zip)
        elif args.cmd == "epub":
            from douban_backup import epub
            epub.build(store, args.dist, args.volumes, full=args.full, per_book=args.per_book)
        elif args.cmd == "update":
            from douban_backup import update
            update.run(store, refresh=args.refresh, fetch=not args.offline, per_book=args.per_book)
        elif args.cmd == "bundle":
            from douban_backup import bundle
            bundle.build(store)
        elif args.cmd == "progress":
            from douban_backup.progress import watch
            watch(store)
        elif args.cmd == "status":
            st = store.state()
            imgs = crawl_images.collect_images(store)
            done = crawl_images.done_map(store)
            print(f"帖子 {args.topic}：已抓 {len(st.get('pages', {}))}/{st.get('total_pages', '?')} 页，"
                  f"{len(store.replies())} 条回复；图片 {len(done)}/{len(imgs)}；"
                  f"下次从 {st.get('cursor', '第 1 页')} 开始"
                  + ("（已到末页，下次抓增量）" if st.get("reached_end") else ""))
            if st.get("refresh"):
                print(f"全量复查 {st['refresh']['id']} 进行中：已查 {len(st['refresh']['pages'])} 页，"
                      "再运行 text --refresh 会接着查")
            elif st.get("last_refresh"):
                lr = st["last_refresh"]
                print(f"上次全量复查：{lr['finished_at']}，{lr['updated']} 条有变化，标记已删除 {lr['deleted']} 条")
            deleted = sum(1 for r in store.replies() if r.get("deleted"))
            if deleted:
                print(f"备份里共有 {deleted} 条回复在原帖已被删除（保留原文）")
    except Blocked as e:
        blocked_exit(e)
    except requests.RequestException as e:
        print(f"\n🌐 网络错误：{type(e).__name__}。已保存的进度不受影响，网络恢复后重新运行同一条命令即可。", file=sys.stderr)
        sys.exit(3)
    except KeyboardInterrupt:
        print("\n已中断。已保存的进度不受影响，下次运行会从断点继续。", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
