# 豆瓣小组长帖备份

把一个豆瓣小组帖子的全部回复（含 GIF 动图、发帖人头像）备份到本地，生成可离线阅读的 HTML；
另外可以生成一份体积小、隐去用户 ID 的**分享版离线包**，发给楼里的朋友，解压后双击就能看。

> 本工具只用于个人留存和小范围分享。备份内容的版权属于原发帖人，请不要公开转载。

## 准备

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Cookie：浏览器登录豆瓣后，从开发者工具 Network 里复制请求头 `Cookie` 的值，存到
`~/.config/douban-backup/cookie.txt`（`chmod 600`），也可以用环境变量 `DOUBAN_COOKIE`。
Cookie 不会被打印，也不在项目目录里，不会进 Git。

## 使用

每一步都可以随时中断，重新运行同一条命令就会从断点继续。

```bash
.venv/bin/python backup.py text --max-pages 40   # 1. 抓文字（本次最多 40 页）
.venv/bin/python backup.py images                # 2. 下载图片（原始文件，GIF 原样保存）
.venv/bin/python backup.py avatars               # 3. 下载发帖人头像
.venv/bin/python backup.py render                # 4. 生成存档版 HTML
.venv/bin/python backup.py share                 # 5. 生成分享版并打包（完整包 + 更新包）
.venv/bin/python backup.py status                # 查看进度（一行）
.venv/bin/python backup.py progress              # 实时进度条（只读，不发请求）
```

- 存档版：打开 `data/<帖子ID>/html/index.html`；地址后加 `#f123` 定位到第 123 楼。
- 其他帖子：所有命令都可以加 `--topic <帖子ID>`。
- `--start 1200` 可以跳过断点，从指定的 `start` 值开始抓。

### 增量更新

帖子还在更新时，隔几天重复 1 → 2 → 3 → 5 即可。抓到末页后，断点会停在末页，下次从末页开始补新回复。
`share` 会对比上次发布的文件清单，额外生成一个只含变化文件的更新包
（`douban-<帖子ID>-update-<上个版本>-to-<本次版本>.zip`），朋友解压到原位置覆盖即可。

## 安全约束

- 单线程；翻页间隔随机 3～6 秒，图片和头像间隔 1～2.5 秒。
- 遇到非 200、重定向（登录页 / sec.douban.com）、页面里没有帖子内容、图片全部 403 时，
  **立刻停止**（退出码 2），已抓内容都已保存，不做重试。
- 网络错误（断网、超时）不是风控信号：等 15 秒、45 秒各重试一次，仍失败再退出（退出码 3）。

## 两个版本的区别

| | 存档版 `data/<帖子ID>/html/` | 分享版 `dist/douban-<帖子ID>/` |
|---|---|---|
| 动图 | 原始 GIF | 转成 MP4，静音循环播放，滚动到时才加载 |
| 图片 | 原始文件 | 长边缩到 1280px，JPEG 质量 80 |
| 发帖人 | 昵称 · 用户 ID 后 4 位 | 昵称 · 本帖内序号（如“momo·84号”），没有用户 ID 和主页链接 |
| 默认昵称“豆友<ID>” | 原样 | 显示为“豆友·N号” |
| 头像、IP 属地、时间、点赞、引用 | 有 | 有 |

序号按每位发帖人在楼里第一次出现的顺序编号，更新后老用户的编号不变。

## 数据目录 `data/<帖子ID>/`

| 文件 | 内容 |
|---|---|
| `replies.jsonl` | 每行一条回复（文字阶段的数据和断点），按回复 ID 去重 |
| `topic.json` | 主楼 |
| `state.json` | 翻页断点 `cursor`、已抓页、总页数 |
| `pages/start_XXXXXX.html` | 原始页面缓存；改解析器后可以直接重新解析，不用再请求 |
| `images/`、`images.jsonl` | 图片原文件和下载记录。GIF 存为 `.gif`；少数动图表情只有视频版，另存 `.mp4` |
| `avatars/`、`avatars.json` | 头像（按内容哈希命名，文件名里没有用户 ID）和下载记录 |
| `share_media/` | 分享版压缩后的素材缓存，只转换新增的 |
| `releases/` | 每次发布的文件清单，用来生成更新包 |

## 图片规则

- 一律下载 `raw` 尺寸（原文件）；`large` / `s` 尺寸会把 GIF 转成静态首帧。
- 候选地址顺序：页面里带签名的原图地址 → 无签名的 `img3.doubanio.com/.../raw/...` → `large`。
- 按文件头判断格式（豆瓣把 GIF 也标成 `.jpg`）；按图片编号去重。

## 楼层号

豆瓣网页不显示楼层号。这里按回复 ID 升序编号（主楼是第 0 楼），删帖不会让已备份的楼层号变化，
但可能和豆瓣 App 里的页码对不上。每条记录也保存了抓取时所在的 `page_start` 和 `page_index`。
