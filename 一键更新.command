#!/bin/bash
# 双击运行：抓新回复 → 生成电脑版、手机版 → 整理上传文件夹（就是 backup.py update）。
# 有新版本时，结束后会自动在访达里打开上传文件夹。
cd "$(dirname "$0")" || exit 1

pause() {
  echo
  read -r -p "按回车关闭这个窗口……"
}

if [ ! -x .venv/bin/python ]; then
  echo "找不到 .venv：请先按 README 的“第一次使用”装好环境。"
  pause
  exit 1
fi

latest=data/500345559/releases/latest.json
version() { grep -o '"version": *"[^"]*"' "$latest" 2>/dev/null | head -1; }
before=$(version)

echo "开始更新（单线程、每页随机等 3～6 秒，楼多时要跑一阵子）。中途可以按 Ctrl+C 停下，下次会接着跑。"
echo
PYTHONUNBUFFERED=1 .venv/bin/python -W ignore backup.py update "$@"
code=$?

echo
if [ $code -eq 0 ]; then
  if [ "$(version)" != "$before" ]; then
    echo "✅ 更新完成，正在打开上传文件夹。把里面的文件夹拖进百度网盘，再删掉网盘里的旧版本。"
    open dist/upload
  else
    echo "没有生成新版本（见上面的说明）。"
  fi
else
  echo "⚠️ 更新没有完成（退出码 $code），请看上面的提示。已保存的进度不受影响，处理好后再双击一次即可。"
fi
pause
