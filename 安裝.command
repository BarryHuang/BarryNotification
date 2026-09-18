#!/bin/bash
# 第一次使用前執行一次，安裝 Playwright 與 Chromium
cd "$(dirname "$0")" || exit 1
echo "安裝 Playwright……"
python3 -m pip install --user playwright || exit 1
echo "下載 Chromium（約 180MB，只需一次）……"
python3 -m playwright install chromium || exit 1
echo
echo "安裝完成。接下來："
echo "  1. 在這個資料夾建立 .env，填入 LINE 金鑰："
echo "       LINE_CLIENT_ID=你的值"
echo "       LINE_CLIENT_SECRET=你的值"
echo "  2. 雙擊「查空房.command」即可執行"
echo
echo "按 Enter 關閉視窗。"
read -r _
