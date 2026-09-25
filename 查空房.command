#!/bin/bash
# 雙擊即可執行：進等候室排隊 → 查空房 → 發 LINE
# 第一次執行請先跑 ./安裝.command 安裝相依套件

cd "$(dirname "$0")" || exit 1

# LINE 金鑰：放在同目錄的 .env（格式見 README），不要 commit 進 git
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

DATES="${1:-}"
if [ -n "$DATES" ]; then
  echo "查詢指定日期：$DATES"
  python3 tokyo_disney_hotel.py --force-notify --dates "$DATES"
else
  echo "查詢所有已開賣的日期"
  python3 tokyo_disney_hotel.py --force-notify
fi

echo
echo "完成。按 Enter 關閉視窗。"
read -r _
