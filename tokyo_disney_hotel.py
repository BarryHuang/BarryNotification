#!/usr/bin/env python3
"""東京迪士尼飯店 2027/01/30~02/01 訂房監控.

住宿日：2027/01/30(六)、2027/01/31(日) 共 2 晚，2027/02/01(一) 退房。

東京迪士尼官方飯店的訂房規則：
  「宿泊日の4ヶ月前の同日 11:00」開始受理，若該月沒有同一天，
  則順延到隔月 1 日 11:00 開始（例：2027/01/31 的 4 個月前是
  2026/09/31，不存在 → 2026/10/01 11:00 開賣）。

因此本次行程的開賣時間：
  - 01/30 (第 1 晚)：2026/09/30 11:00 JST
  - 01/31 (第 2 晚)：2026/10/01 11:00 JST
  - 連住 2 晚一次訂完：2026/10/01 11:00 JST（以較晚的那晚為準）

腳本行為：
  1. 開賣前 → 只做開賣倒數提醒（不打官網，避免無意義流量）。
  2. 開賣後 → 用 Playwright 逐間飯店查詢空房：
     先查「連住 2 晚」，若客滿再查單晚 01/30 / 01/31（可分段搶、之後再合併）。
  3. 結果透過 LINE 廣播（與本專案其他監控相同）。

註：官網有 Akamai 反爬蟲，可能回應 Access Denied；此時會標記為
「無法判斷」並附上連結請手動確認，不會誤報成客滿。
"""
import os
import sys
import re
import json
import ssl
import datetime
import urllib.request
import urllib.parse

# === Config ===
JST = datetime.timezone(datetime.timedelta(hours=9))
TW = datetime.timezone(datetime.timedelta(hours=8))

CHECKIN_DATE = datetime.date(2027, 1, 30)
CHECKOUT_DATE = datetime.date(2027, 2, 1)
STAY_NIGHTS = [datetime.date(2027, 1, 30), datetime.date(2027, 1, 31)]

ROOMS_NUM = 1
ADULT_NUM = 2
CHILD_NUM = 0

# searchHotelCD 對照（迪士尼直營飯店）
HOTELS = [
    ("DHM", "東京迪士尼海洋觀海景大飯店 MiraCosta"),
    ("FSH", "東京迪士尼海洋夢幻泉鄉夢幻飯店 Fantasy Springs"),
    ("TDH", "東京迪士尼樂園大飯店"),
    ("DAH", "迪士尼大使大飯店"),
    ("TSH", "東京迪士尼玩具總動員飯店"),
    ("DCH", "東京迪士尼慶典飯店"),
]

SEARCH_BASE = "https://reserve.tokyodisneyresort.jp/hotel/list/"
RESERVE_TOP = "https://reserve.tokyodisneyresort.jp/hotel/search/"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
PAGE_TIMEOUT_MS = 60000

LINE_CLIENT_ID = os.environ.get("LINE_CLIENT_ID")
LINE_CLIENT_SECRET = os.environ.get("LINE_CLIENT_SECRET")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

WEEKDAY_TW = ["一", "二", "三", "四", "五", "六", "日"]


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


LINE_TEXT_LIMIT = 4900   # 單則訊息上限 5000 字，留一點餘裕
LINE_MAX_MESSAGES = 5    # 一次 broadcast 最多 5 則


def split_for_line(text):
    """依行切成多則訊息，避免超過 LINE 單則字數上限。"""
    chunks, buf = [], ""
    for line in text.split("\n"):
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > LINE_TEXT_LIMIT and buf:
            chunks.append(buf)
            buf = line[:LINE_TEXT_LIMIT]
        else:
            buf = candidate[:LINE_TEXT_LIMIT]
    if buf:
        chunks.append(buf)
    if len(chunks) > LINE_MAX_MESSAGES:
        chunks = chunks[:LINE_MAX_MESSAGES]
        chunks[-1] = chunks[-1][:LINE_TEXT_LIMIT - 40] + "\n…（內容過長，其餘請見官網）"
    return chunks


def send_line_broadcast(token, text):
    url = "https://api.line.me/v2/bot/message/broadcast"
    messages = [{"type": "text", "text": chunk} for chunk in split_for_line(text)]
    payload = json.dumps({"messages": messages}).encode('utf-8')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, context=ctx) as response:
            print(f"LINE broadcast sent successfully ({len(messages)} message(s)).")
    except Exception as e:
        print(f"Failed to send LINE message: {e}")


# === 開賣時間計算 ===

def four_months_before(stay_date):
    """回傳宿泊日 4 個月前的 (年, 月)。"""
    year = stay_date.year
    month = stay_date.month - 4
    while month <= 0:
        month += 12
        year -= 1
    return year, month


def reservation_open_at(stay_date):
    """宿泊日的 4 個月前同日 11:00 JST；該月無同一天則順延到隔月 1 日 11:00。"""
    year, month = four_months_before(stay_date)
    try:
        open_day = datetime.date(year, month, stay_date.day)
    except ValueError:
        # 例：2026/09/31 不存在 → 隔月 1 日
        if month == 12:
            open_day = datetime.date(year + 1, 1, 1)
        else:
            open_day = datetime.date(year, month + 1, 1)
    return datetime.datetime.combine(open_day, datetime.time(11, 0), tzinfo=JST)


def fmt_dt(dt):
    """以日本時間 + 台灣時間並列顯示。"""
    jst = dt.astimezone(JST)
    tw = dt.astimezone(TW)
    return (
        f"{jst:%Y/%m/%d %H:%M} 日本時間"
        f"（台灣 {tw:%m/%d %H:%M}）"
    )


def fmt_date(d):
    return f"{d:%Y/%m/%d}({WEEKDAY_TW[d.weekday()]})"


def humanize_delta(delta):
    total = int(delta.total_seconds())
    if total <= 0:
        return "已開賣"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"還有 {days} 天 {hours} 小時"
    if hours:
        return f"還有 {hours} 小時 {minutes} 分"
    return f"還有 {minutes} 分"


# === 空房查詢 ===

def build_search_url(hotel_cd, use_date, staying_days):
    params = [
        ("showWay", ""),
        ("roomsNum", str(ROOMS_NUM)),
        ("adultNum", str(ADULT_NUM)),
        ("childNum", str(CHILD_NUM)),
        ("stayingDays", str(staying_days)),
        ("useDate", use_date.strftime("%Y%m%d")),
        ("cpListStr", ""),
        ("childAgeBedInform", ""),
        ("searchHotelCD", hotel_cd),
        ("searchHotelDiv", ""),
        ("hotelName", ""),
        ("searchHotelName", ""),
        ("searchLayer", ""),
        ("searchRoomName", ""),
        ("hotelSearchDetail", "true"),
        ("detailOpenFlg", "0"),
        ("checkPointStr", ""),
        ("hotelChangeFlg", "false"),
        ("removeSessionFlg", "true"),
        ("returnFlg", "false"),
        ("hotelShowFlg", ""),
        ("displayType", "data-hotel"),
        ("reservationStatus", "1"),
    ]
    return SEARCH_BASE + "?" + urllib.parse.urlencode(params)


SOLD_OUT_PATTERNS = [
    "満室",
    "空室がございません",
    "空室はございません",
    "条件に合う",
    "該当する客室",
    "見つかりませんでした",
    "ございませんでした",
]
NOT_OPEN_PATTERNS = [
    "予約受付期間外",
    "受け付けておりません",
    "受付前",
    "予約受付開始",
]
BLOCKED_PATTERNS = [
    "access denied",
    "アクセスが集中",
    "只今大変混み合って",
    "ただいまアクセスが",
    "reference #",
]


def classify_page(text):
    """回傳 (status, detail)。status: AVAILABLE / SOLD_OUT / NOT_OPEN / BLOCKED / UNKNOWN"""
    lower = text.lower()

    for pat in BLOCKED_PATTERNS:
        if pat in lower or pat in text:
            return "BLOCKED", "官網擋下自動查詢（反爬蟲），請手動確認"

    # 有價格 + 可選房動作 → 視為有空房
    prices = re.findall(r'[\d,]{3,}\s*円', text)
    has_action = any(k in text for k in ("空室あり", "残り", "このプランで予約", "客室を選ぶ", "プランを選ぶ"))

    for pat in NOT_OPEN_PATTERNS:
        if pat in text:
            return "NOT_OPEN", "官網顯示尚未開放此日期的預約"

    if prices and has_action:
        sample = "、".join(prices[:3])
        return "AVAILABLE", f"出現方案價格（例：{sample}）"
    if prices:
        sample = "、".join(prices[:3])
        return "AVAILABLE", f"出現價格資訊（例：{sample}），請儘速確認"

    for pat in SOLD_OUT_PATTERNS:
        if pat in text:
            return "SOLD_OUT", "客滿／查無符合條件的空房"

    return "UNKNOWN", "頁面無明確訊息，版面可能已變更"


def check_hotel(page, hotel_cd, use_date, staying_days):
    url = build_search_url(hotel_cd, use_date, staying_days)
    try:
        page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(2500)
        text = page.inner_text("body")
    except Exception as e:
        print(f"  [{hotel_cd}] page load error: {e}")
        return {"status": "UNKNOWN", "detail": f"頁面載入失敗：{e}", "url": url}

    status, detail = classify_page(text)
    print(f"  [{hotel_cd}] {use_date} x{staying_days}晚 → {status} ({detail})")
    return {"status": status, "detail": detail, "url": url}


def run_searches(searches):
    """searches: [(label, use_date, staying_days)]，回傳 {label: [(hotel_name, result)]}"""
    # 開賣前不需要瀏覽器，所以延後 import，讓倒數模式在沒裝 Playwright 時也能跑。
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=BROWSER_UA,
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        for label, use_date, staying_days in searches:
            print(f"[查詢] {label}")
            rows = []
            for hotel_cd, hotel_name in HOTELS:
                rows.append((hotel_name, check_hotel(page, hotel_cd, use_date, staying_days)))
            results[label] = rows
        browser.close()
    return results


STATUS_LABEL = {
    "AVAILABLE": "🎉 有空房！",
    "SOLD_OUT": "客滿",
    "NOT_OPEN": "尚未開賣",
    "BLOCKED": "⚠️ 官網擋查詢",
    "UNKNOWN": "⚠️ 無法判斷",
}


def format_rows(rows):
    lines = []
    for hotel_name, res in rows:
        label = STATUS_LABEL.get(res["status"], res["status"])
        if res["status"] == "AVAILABLE":
            lines.append(f"  ✅ {hotel_name}：{label}")
            lines.append(f"     {res['detail']}")
            lines.append(f"     🔗 {res['url']}")
        elif res["status"] in ("BLOCKED", "UNKNOWN"):
            lines.append(f"  ❔ {hotel_name}：{label}（{res['detail']}）")
        else:
            lines.append(f"  ・{hotel_name}：{label}")
    return "\n".join(lines)


# === Main ===

def main():
    send_line = "--no-line" not in sys.argv

    now = datetime.datetime.now(tz=JST)
    now_text = now.strftime("%Y-%m-%d %H:%M JST")

    open_by_night = [(d, reservation_open_at(d)) for d in STAY_NIGHTS]
    combined_open = max(t for _, t in open_by_night)

    # --- 開賣時程 ---
    schedule_lines = []
    for d, open_at in open_by_night:
        schedule_lines.append(
            f"  ・{fmt_date(d)} 這晚：{fmt_dt(open_at)}｜{humanize_delta(open_at - now)}"
        )
    schedule_text = "\n".join(schedule_lines)

    # 若有哪一晚因為「4 個月前該月無同一天」而順延，附上說明
    rollover_lines = []
    for d, open_at in open_by_night:
        if open_at.day != d.day:
            y, mo = four_months_before(d)
            rollover_lines.append(
                f"    （{y}/{mo}/{d.day} 不存在，"
                f"{d:%m/%d} 那晚順延到 {open_at:%Y/%m/%d} 開賣）"
            )
    rollover_text = ("\n".join(rollover_lines) + "\n") if rollover_lines else ""

    # --- 決定要跑哪些查詢 ---
    searches = []
    if now >= combined_open:
        searches.append((
            f"連住 2 晚（{fmt_date(CHECKIN_DATE)} 入住 → {fmt_date(CHECKOUT_DATE)} 退房）",
            CHECKIN_DATE, 2,
        ))
    for d, open_at in open_by_night:
        if now >= open_at:
            searches.append((f"單晚 {fmt_date(d)}", d, 1))

    urgent = False
    if searches:
        print(f"開賣中，執行 {len(searches)} 組查詢……")
        results = run_searches(searches)
        blocks = []
        for label, _, _ in searches:
            rows = results[label]
            if any(r["status"] == "AVAILABLE" for _, r in rows):
                urgent = True
            blocks.append(f"【{label}】\n{format_rows(rows)}")
        result_text = "\n\n".join(blocks)
    else:
        soonest = min(t for _, t in open_by_night)
        result_text = (
            "尚未開賣，今天不查詢官網。\n"
            f"最快可訂的一晚是 {fmt_date(STAY_NIGHTS[0])}，"
            f"{humanize_delta(soonest - now)}開賣。"
        )
        if soonest - now <= datetime.timedelta(days=1):
            urgent = True

    if now < combined_open and combined_open - now <= datetime.timedelta(days=1):
        urgent = True

    header = "🚨🚨 " if urgent else ""

    body = (
        f"{header}🏰 東京迪士尼飯店訂房監控\n"
        f"📅 報告時間：{now_text}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"🛏 目標行程：{fmt_date(CHECKIN_DATE)} 入住 → {fmt_date(CHECKOUT_DATE)} 退房\n"
        f"　（住 {len(STAY_NIGHTS)} 晚：{fmt_date(STAY_NIGHTS[0])}、{fmt_date(STAY_NIGHTS[1])}）\n"
        f"　{ADULT_NUM} 位大人 / {ROOMS_NUM} 間房\n"
        f"\n"
        f"⏰ 官網開賣時間（宿泊日 4 個月前同日 11:00 日本時間）\n"
        f"{schedule_text}\n"
        f"  ★ 兩晚一次訂完要等到：{fmt_dt(combined_open)}\n"
        f"{rollover_text}"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"{result_text}\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔗 官網訂房：{RESERVE_TOP}\n"
        f"🤖 此為自動化播報服務 (GitHub Actions)"
    )

    print("Execution result:")
    print(body)

    if not send_line:
        print("--no-line 指定，略過 LINE 廣播。")
        return

    line_token = get_line_token()
    if line_token:
        send_line_broadcast(line_token, body)
    else:
        print("Skipping LINE broadcast as tokens are not configured.")


if __name__ == "__main__":
    main()
