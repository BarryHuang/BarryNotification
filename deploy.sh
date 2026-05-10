#!/bin/bash
# 一鍵推送到 GitHub
# 用法：./deploy.sh
cd "$(dirname "$0")"
echo "🚀 推送到 GitHub..."
git push origin main && echo "✅ 推送成功！" || echo "❌ 推送失敗，請檢查網路或憑證。"
