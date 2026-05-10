#!/bin/bash
cd "$(dirname "$0")"
echo "🚀 正在推送到 GitHub..."
git push origin main
if [ $? -eq 0 ]; then
    echo "✅ 推送成功！"
else
    echo "❌ 推送失敗"
fi
echo ""
echo "按任意鍵關閉..."
read -n 1
