#!/usr/bin/env python3
"""Sierra Resort Hakuba 訂房開放監控.

每天檢查 https://go-sierraresort.reservation.jp 是否已開放
2027/01/22 ~ 2027/01/30 的訂房，並透過 LINE 廣播結果。

頁面為 Livewire (JavaScript) 動態渲染，需用 Playwright headless browser 載入。
"""
import os
import datetime
import json
import ssl
import urllib.request
import urllib.parse

from playwright.sync_api import sync_playwright

# === Config ===
CHECKIN_DATE = "20270122"
CHECKOUT_DATE = "20270130"

SEARCH_URL = (
    "https://go-sierraresort.reservation.jp/en/hotels/sierra-hakuba/plans"
    f"?checkin_date={CHECKIN_DATE}&checkout_date={CHECKOUT_DATE}"
    "&room_num=1&adult_num=2&child_high_num=0&child_middle_num=0&child_low_num=0"
)

LINE_CLIENT_ID = os.environ.get("LINE_CLIENT_ID")
LINE_CLIENT_SECRET = os.environ.get("LINE_CLIENT_SECRET")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


# === LINE Messaging ===

def get_line_token():
    if not LINE_CLIENT_ID or not LINE_CLIENT_SECRET:
        print("Missing LINE credentials in environment variables.")
        return None
    url = "https://api.line.me/v2/oauth/accessToken"
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": LINE_CLIENT_ID,
        "client_secret": LINE_CLIENT_SECRET
    }).encode('utf-8')
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, context=ctx) as response:
            resp_data = json.loads(response.read().decode('utf-8'))
            return resp_data.get("access_token")
    except Exception as e:
        print(f"Failed to get LINE token: {e}")
        return None


def send_line_broadcast(token, text):
    url = "https://api.line.me/v2/bot/message/broadcast"
    payload = json.dumps({
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }).encode('utf-8')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, context=ctx) as response:
            print("LINE broadcast sent successfully.")
    except Exception as e:
        print(f"Failed to send LINE message: {e}")


# === Sierra Hakuba Booking Monitor ===

def check_sierra_hakuba():
    """
    回傳 {"status": "OPEN"|"NOT_OPEN"|"UNKNOWN", "excerpt": str}
    - OPEN: 搜尋結果列出可訂方案（頁面出現價格/方案數量）
    - NOT_OPEN: 頁面顯示 "There are no offer(s) available for reservation"
    - UNKNOWN: 頁面載入失敗或版面改變，無法判斷
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
            # Livewire 在初始載入後才渲染方案列表，多等一下
            page.wait_for_timeout(3000)
            body = page.inner_text("body")
            browser.close()
    except Exception as e:
        print(f"Sierra Hakuba page load error: {e}")
        return {"status": "UNKNOWN", "excerpt": str(e)}

    lower = body.lower()
    has_price = "jpy" in lower or "¥" in body or "yen" in lower
    not_open = "no offer(s) available" in lower

    if has_price:
        status = "OPEN"
    elif not_open:
        status = "NOT_OPEN"
    else:
        status = "UNKNOWN"

    # 擷取搜尋結果區塊（從日期條件開始）供通知與除錯使用
    idx = body.find("1/22")
    excerpt = body[idx:idx + 600] if idx != -1 else body[:600]
    return {"status": status, "excerpt": excerpt}


# === Main ===

def main():
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    result = check_sierra_hakuba()
    print(f"Sierra Hakuba check result: {result['status']}")
    print(result['excerpt'])

    if result["status"] == "OPEN":
        status_text = (
            "🎉🎉 已開放訂房！快去搶房！ 🎉🎉\n"
            "【搜尋結果摘要】\n"
            f"{result['excerpt']}"
        )
    elif result["status"] == "NOT_OPEN":
        status_text = "尚未開放訂房（顯示 no offers available）"
    else:
        status_text = "⚠️ 無法判斷，請以網頁實際狀況為主。"

    body = (
        f"📅 報告時間：{today}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"🎿 Sierra Resort Hakuba 訂房監控\n"
        f"【2027/01/22 (五) ~ 01/30 (六)，2 大人 1 房】\n"
        f"{status_text}\n"
        f"🔗 {SEARCH_URL}\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🤖 此為自動化播報服務 (GitHub Actions)"
    )

    print("Execution result:")
    print(body)

    line_token = get_line_token()
    if line_token:
        send_line_broadcast(line_token, body)
    else:
        print("Skipping LINE broadcast as tokens are not configured.")


if __name__ == "__main__":
    main()
