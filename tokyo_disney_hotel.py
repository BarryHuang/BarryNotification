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

# 監控區間：2027/01/01 ~ 01/31，逐日單晚查詢（不綁連住）
TARGET_START = datetime.date(2027, 1, 1)
TARGET_END = datetime.date(2027, 1, 31)
STAY_NIGHTS_PER_SEARCH = 1

# 只監控這幾間（全 6 間請求量太大，容易被反爬蟲擋）
MONITOR_HOTELS = ["FSH", "DHM"]

# 每日總覽在日本時間這個鐘點那一輪發送
DIGEST_HOUR_JST = 8

# 總覽裡最多詳列幾筆空房（含房型與連結）。LINE 單則有字數上限，
# 空房很多時只詳列前幾筆，其餘用精簡一行帶過。
DIGEST_DETAIL_LIMIT = 8

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
# 導覽逾時。官網是 JS 動態渲染，實測 networkidle 永遠等不到
# （30/30 全部 60 秒逾時），所以改成等 DOM 就緒 + 輪詢內容出現。
PAGE_TIMEOUT_MS = 30000

# DOM 就緒後，最多再等這麼久讓搜尋結果渲染出來
RESULT_WAIT_MS = 15000

# 頁面出現這些字樣之一，就視為結果已經渲染完成
READY_MARKERS = (
    "円", "満室", "空室", "プラン", "予約",
    "見つかりませんでした", "受け付けておりません",
)

# 官網忙碌時會把人送進 Queue-it 等候室。排隊本來就該讓程式去等，
# 所以遇到等候室就掛在那裡直到放行（Queue-it 會自己轉址回來）。
# 放行後 cookie 會留在同一個瀏覽器 context，後續日期不必重新排隊。
QUEUE_MAX_WAIT_MS = 20 * 60 * 1000   # 最多陪等 20 分鐘
QUEUE_POLL_MS = 5000

# 診斷輸出：頁面判讀不出來時，印出前幾筆的實際內容供除錯
DIAGNOSTIC_SAMPLES = 2

# 連續這麼多筆都讀不到就中止整輪掃描。
# 官網掛掉或擋我們時，沒必要把剩下幾十個頁面各等滿逾時
# （那會讓單輪跑掉數十分鐘，甚至超過 workflow 的時間上限）。
ABORT_AFTER_CONSECUTIVE_FAILURES = 6

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

def target_dates():
    """監控區間內的所有日期。"""
    span = (TARGET_END - TARGET_START).days + 1
    return [TARGET_START + datetime.timedelta(days=i) for i in range(span)]


def open_target_dates(now):
    """區間內「已經開賣」的日期。還沒開賣的不查，省下無謂的請求。"""
    return [d for d in target_dates() if now >= reservation_open_at(d)]


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
# 官網忙碌時會把人丟到 reserve-q.tokyodisneyresort.jp 的等候室，
# 那是一般的排隊機制，不是被擋，也不代表沒空房，要跟「讀不到」區分開。
QUEUE_PATTERNS = (
    "temporarily busy",
    "access the page in order",
    "reserve-q.tokyodisneyresort.jp",
    "順番",
    "お待ちください",
    "しばらくお待ち",
)

# 每天 3:00~5:00 JST 系統維護，站台本來就不開放
MAINTENANCE_PATTERNS = (
    "システムメンテナンス",
    "system maintenance",
    "メンテナンス中",
)

BLOCKED_PATTERNS = [
    "access denied",
    "アクセスが集中",
    "只今大変混み合って",
    "ただいまアクセスが",
    "reference #",
]


def classify_page(text, url=""):
    """回傳 (status, detail)。

    status: AVAILABLE / SOLD_OUT / NOT_OPEN / QUEUED / MAINTENANCE / BLOCKED / UNKNOWN
    """
    lower = text.lower()
    lower_url = (url or "").lower()

    # 排隊與維護要先判，否則會被當成「版面改了讀不到」
    if "reserve-q." in lower_url or any(
            pat in lower or pat in text for pat in QUEUE_PATTERNS):
        return "QUEUED", "官網忙碌中，被導到等候室排隊"

    if any(pat in lower or pat in text for pat in MAINTENANCE_PATTERNS):
        return "MAINTENANCE", "官網系統維護中（每天 3:00~5:00 JST）"

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


# === 房型擷取 ===

# 官方房型介紹頁（FSH 分兩棟，各給一條）
ROOM_PAGES = {
    "FSH": [
        ("ファンタジーシャトー", "https://www.tokyodisneyresort.jp/hotel/fsh/fcu/room.html"),
        ("グランドシャトー", "https://www.tokyodisneyresort.jp/hotel/fsh/gcu/room.html"),
    ],
    "DHM": [("客室一覧", "https://www.tokyodisneyresort.jp/hotel/dhm/room.html")],
    "TDH": [("客室一覧", "https://www.tokyodisneyresort.jp/hotel/tdh/room.html")],
    "DAH": [("客室一覧", "https://www.tokyodisneyresort.jp/hotel/dah/room.html")],
    "TSH": [("客室一覧", "https://www.tokyodisneyresort.jp/hotel/tsh/room.html")],
    "DCH": [("客室一覧", "https://www.tokyodisneyresort.jp/hotel/dch/room.html")],
}

# 房型名稱的構造：〔サイド〕＋〔片假名/英數〕＋ルーム|スイート|キャビン＋〔（○○ビュー）〕
# 用結構比對而不是寫死清單，迪士尼改名或新增房型時比較不會失效。
ROOM_NAME_RE = re.compile(
    r'(?:[ァ-ヶー・]{2,20}(?:サイド|シャトー)\s*)?'   # ○○サイド／○○シャトー
    r'[ァ-ヶーA-Za-z0-9・＆&]{2,30}?'
    r'(?:ルーム|スイート|キャビン)'
    r'(?:\s*[＆&]\s*スイート)?'                      # スペチアーレ・ルーム＆スイート
    r'(?:\s*[（(][^）)\n]{1,30}[）)])?'              # （ハーバービュー）
)

# 這些字樣出現在房型名稱裡通常是導覽列或說明，不是真的房型
ROOM_NAME_BLOCKLIST = ("客室一覧", "の客室", "客室タイプ", "ルームサービス", "ルームキー")

CARD_SELECTORS = [
    "[class*='planList'] li",
    "[class*='roomList'] li",
    "[class*='searchResult'] li",
    "li[class*='plan']",
    "li[class*='room']",
    "[class*='planUnit']",
    "[class*='roomUnit']",
    "article",
]

PRICE_RE = re.compile(r'([\d][\d,]{2,})\s*円')


def extract_prices(text):
    """抓出頁面上的日圓金額，濾掉明顯不是房價的數字，回傳排序後的 list[int]。"""
    values = []
    for raw in PRICE_RE.findall(text):
        try:
            value = int(raw.replace(",", ""))
        except ValueError:
            continue
        # 迪士尼飯店一晚大約 3 萬 ~ 30 萬日圓；其餘多半是點數、稅金、說明文字
        if 10000 <= value <= 1000000:
            values.append(value)
    return sorted(set(values))


def in_queue(page):
    """目前是否被擋在 Queue-it 等候室。"""
    try:
        if "reserve-q." in (page.url or "").lower():
            return True
        text = page.inner_text("body")
    except Exception:
        return False
    lower = text.lower()
    return any(pat in lower or pat in text for pat in QUEUE_PATTERNS)


def wait_out_queue(page, url):
    """在等候室排隊直到放行。

    Queue-it 輪到就會自己轉址回官網，所以這裡只要等著。
    這正是自動化該做的事——讓程式去等，而不是讓人盯著進度條。
    回傳 True 表示已放行，False 表示等超過上限。
    """
    if not in_queue(page):
        return True

    print(f"  ⏳ 進入等候室排隊（最多等 {QUEUE_MAX_WAIT_MS // 60000} 分鐘）……")
    waited = 0
    while waited < QUEUE_MAX_WAIT_MS:
        page.wait_for_timeout(QUEUE_POLL_MS)
        waited += QUEUE_POLL_MS
        if not in_queue(page):
            print(f"  ✅ 排隊結束（等了 {waited // 1000} 秒），繼續查詢。")
            # 放行後可能停在首頁，重新導向原本要查的網址
            try:
                if "hotel/list" not in (page.url or ""):
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=PAGE_TIMEOUT_MS)
            except Exception as e:
                print(f"  ! 排隊後重新載入失敗：{str(e).splitlines()[0]}")
            return True
        if waited % 60000 == 0:
            print(f"     仍在排隊……已等 {waited // 60000} 分鐘")

    print(f"  ⚠️ 排隊超過 {QUEUE_MAX_WAIT_MS // 60000} 分鐘仍未放行，放棄這一輪。")
    return False


def load_search_page(page, url):
    """載入搜尋結果頁並回傳頁面文字。

    不使用 wait_until="networkidle"：官網背景有持續的請求，這個條件
    實際上永遠不會成立（實測整批 60 秒逾時）。改為等 DOM 就緒，
    再輪詢頁面文字直到出現價格／滿室等關鍵字。

    若被送進等候室，會先排完隊再繼續。
    """
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

    if not wait_out_queue(page, url):
        return page.inner_text("body") if page else ""

    waited = 0
    text = ""
    while waited < RESULT_WAIT_MS:
        try:
            text = page.inner_text("body")
        except Exception:
            text = ""
        if any(marker in text for marker in READY_MARKERS):
            return text
        page.wait_for_timeout(500)
        waited += 500

    # 等不到關鍵字也把目前的內容交出去，交給 classify_page 判斷
    return text


def describe_page(page, text, label):
    """把頁面實際長相印進 log，方便在看不到網站的情況下除錯。"""
    try:
        title = page.title()
    except Exception:
        title = "(取不到標題)"
    try:
        url = page.url
    except Exception:
        url = "(取不到網址)"
    snippet = " ".join((text or "").split())[:300]
    print(f"  [診斷] {label}")
    print(f"    title: {title}")
    print(f"    url:   {url}")
    print(f"    text:  {snippet or '(頁面沒有任何文字)'}")


def find_room_names(text):
    """從一段文字裡找出看起來像房型名稱的字串（保持出現順序、去重）。"""
    names = []
    for match in ROOM_NAME_RE.finditer(text):
        name = " ".join(match.group(0).split())
        if any(bad in name for bad in ROOM_NAME_BLOCKLIST):
            continue
        if name not in names:
            names.append(name)
    return names


def extract_offers(page, text):
    """擷取「房型 + 價格」。

    官網頁面結構未知且會改版，所以採兩段式：
    先嘗試把搜尋結果拆成一張張卡片逐張讀；失敗就退回整頁文字配對。
    兩者都抓不到時回傳空 list，呼叫端仍會附上訂房連結讓人自己看。
    """
    offers = []

    for selector in CARD_SELECTORS:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue
        # 數量太誇張的多半選到版面容器，不是方案卡片
        if not elements or len(elements) > 80:
            continue
        for element in elements:
            try:
                card_text = element.inner_text()
            except Exception:
                continue
            names = find_room_names(card_text)
            prices = extract_prices(card_text)
            if names and prices:
                offers.append({"room": names[0], "price": prices[0]})
        if offers:
            break

    if not offers:
        # 退路：逐行掃全頁文字，房型名稱後面幾行內的第一個價格就當作它的價格
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            names = find_room_names(line)
            if not names:
                continue
            price = None
            for follow in lines[idx:idx + 6]:
                found = extract_prices(follow)
                if found:
                    price = found[0]
                    break
            offers.append({"room": names[0], "price": price})

    # 同房型只留最便宜的一筆
    best = {}
    for offer in offers:
        room, price = offer["room"], offer["price"]
        if room not in best or (price is not None and
                                (best[room] is None or price < best[room])):
            best[room] = price
    return [{"room": r, "price": p} for r, p in best.items()]


def room_page_links(hotel_cd):
    return ROOM_PAGES.get(hotel_cd, [])


def scan_range(hotel_codes, start_date, days, staying_days=1, delay_ms=2500):
    """--scan 模式用：掃描從 start_date 起連續 days 天。"""
    dates = [start_date + datetime.timedelta(days=i) for i in range(days)]
    return scan_dates(hotel_codes, dates, staying_days, delay_ms)


def scan_dates(hotel_codes, dates, staying_days=1, delay_ms=2500):
    """逐日掃描指定飯店的空房與價格。

    回傳 [{key, date, hotel_cd, hotel_name, status, prices, url}, ...]
    每次查詢之間會停頓 delay_ms，避免對官網造成壓力而被擋。
    """
    from playwright.sync_api import sync_playwright

    hotel_names = dict(HOTELS)
    rows = []
    diagnosed = [0]
    consecutive_failures = 0
    aborted = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=BROWSER_UA,
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        for use_date in dates:
            if aborted:
                break
            for hotel_cd in hotel_codes:
                url = build_search_url(hotel_cd, use_date, staying_days)
                prices, offers = [], []
                text = ""
                try:
                    text = load_search_page(page, url)
                    status, _ = classify_page(text, page.url)
                    prices = extract_prices(text)
                    if status == "AVAILABLE":
                        offers = extract_offers(page, text)
                except Exception as e:
                    status = "UNKNOWN"
                    print(f"  ! {use_date} {hotel_cd} 載入失敗："
                          f"{str(e).splitlines()[0]}")

                if status not in ("AVAILABLE", "SOLD_OUT", "NOT_OPEN") \
                        and diagnosed[0] < DIAGNOSTIC_SAMPLES:
                    diagnosed[0] += 1
                    describe_page(page, text, f"{use_date} {hotel_cd} 判讀為 {status}")

                rows.append({
                    "key": f"{use_date}|{hotel_cd}",
                    "date": use_date,
                    "hotel_cd": hotel_cd,
                    "hotel_name": hotel_names.get(hotel_cd, hotel_cd),
                    "status": status,
                    "prices": prices,
                    "offers": offers,
                    "url": url,
                })
                if status in ("UNKNOWN", "BLOCKED", "QUEUED", "MAINTENANCE"):
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

                cheapest = f"{prices[0]:,} 円起" if prices else "-"
                rooms = f" [{len(offers)} 種房型]" if offers else ""
                print(f"  {use_date} ({WEEKDAY_TW[use_date.weekday()]}) {hotel_cd}: "
                      f"{status:9} {cheapest}{rooms}")
                for offer in offers:
                    price = f"{offer['price']:,} 円" if offer["price"] else "價格未擷取"
                    print(f"      - {offer['room']}：{price}")

                if consecutive_failures >= ABORT_AFTER_CONSECUTIVE_FAILURES:
                    print(f"  ⚠️ 連續 {consecutive_failures} 筆讀不到，中止這一輪掃描"
                          f"（已查 {len(rows)} / {len(dates) * len(hotel_codes)} 筆）。")
                    aborted = True
                    break

                page.wait_for_timeout(delay_ms)
        browser.close()
    return rows


def render_scan_report(rows, start_date, days, staying_days):
    """把掃描結果排成一張表。"""
    hotel_codes = []
    for row in rows:
        if row["hotel_cd"] not in hotel_codes:
            hotel_codes.append(row["hotel_cd"])
    hotel_names = dict(HOTELS)
    by_key = {(r["date"], r["hotel_cd"]): r for r in rows}

    mark = {"AVAILABLE": "O", "SOLD_OUT": "X", "NOT_OPEN": "-", "BLOCKED": "?", "UNKNOWN": "?"}

    lines = []
    lines.append(f"掃描區間：{start_date} 起 {days} 天，每次 {staying_days} 晚，"
                 f"{ADULT_NUM} 大人 / {ROOMS_NUM} 房")
    lines.append("")
    header = f"{'日期':<14}"
    for cd in hotel_codes:
        header += f"{cd:>22}"
    lines.append(header)
    lines.append("-" * len(header))

    for offset in range(days):
        d = start_date + datetime.timedelta(days=offset)
        line = f"{d:%m/%d}({WEEKDAY_TW[d.weekday()]})    "
        for cd in hotel_codes:
            row = by_key.get((d, cd))
            if not row:
                line += f"{'':>22}"
                continue
            flag = mark.get(row["status"], "?")
            price = f"{row['prices'][0]:,}円" if row["prices"] else ""
            line += f"{flag + ' ' + price:>22}"
        lines.append(line)

    lines.append("")
    lines.append("O=有空房  X=客滿  -=未開放  ?=無法判斷（官網擋查詢或版面改變）")
    for cd in hotel_codes:
        lines.append(f"  {cd} = {hotel_names.get(cd, cd)}")

    available = [r for r in rows if r["status"] == "AVAILABLE"]
    lines.append("")
    if available:
        lines.append(f"== 有空房的日期（共 {len(available)} 筆）==")
        for r in sorted(available, key=lambda r: (r["date"], r["hotel_cd"])):
            price = f"{r['prices'][0]:,} 円起" if r["prices"] else "價格未擷取到"
            lines.append(f"  {r['date']} ({WEEKDAY_TW[r['date'].weekday()]}) "
                         f"{r['hotel_name']}：{price}")
            lines.append(f"    {r['url']}")
    else:
        lines.append("== 這個區間沒有掃到任何空房 ==")

    blocked = [r for r in rows if r["status"] in ("BLOCKED", "UNKNOWN")]
    if blocked:
        lines.append("")
        lines.append(f"⚠️ 有 {len(blocked)} 筆無法判斷（官網可能擋下自動查詢），請以官網為準。")

    return "\n".join(lines)


def run_scan(argv):
    """--scan 模式：掃描一段期間的空房與價格，只輸出到 log，不發 LINE。"""
    def arg(name, default):
        if name in argv:
            return argv[argv.index(name) + 1]
        return default

    start_raw = arg("--start", None)
    start_date = (datetime.datetime.strptime(start_raw, "%Y-%m-%d").date()
                  if start_raw else datetime.datetime.now(tz=JST).date())
    days = int(arg("--days", "30"))
    staying_days = int(arg("--nights", "1"))
    codes = arg("--hotels", "FSH,DHM").split(",")

    print(f"開始掃描：{start_date} 起 {days} 天，飯店 {codes}，每次 {staying_days} 晚")
    rows = scan_range(codes, start_date, days, staying_days)
    report = render_scan_report(rows, start_date, days, staying_days)
    print()
    print("===== SCAN REPORT =====")
    print(report)
    print("===== END REPORT =====")


# === 狀態檔（只在「新出現空房」時才通知，避免每輪洗版）===

STATE_PATH = os.path.join("docs", "data", "disney_seen.json")

# 連續幾次都讀不到官網就發一次「監控壞掉」警告。
# 平常沒消息代表沒空房，所以監控啞掉一定要讓人知道。
BLOCKED_ALERT_THRESHOLD = 12


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    """只有實質內容變動時才寫檔。

    定期跑的排程如果連時間戳都寫進去，會不斷產生沒有意義的 commit。
    """
    payload = {k: v for k, v in state.items() if k != "updated"}
    old_payload = {k: v for k, v in load_state().items() if k != "updated"}
    if payload == old_payload:
        print("狀態沒有變化，不改寫狀態檔。")
        return False

    parent = os.path.dirname(STATE_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    return True


def diff_availability(rows, prev_available):
    """比對本次與上次的空房狀態。

    回傳 (now_available, newly_available, definitive_count)。
    查詢被擋或判讀不出來時「沿用上次的狀態」，避免一次誤判就
    把房間當成消失、下次又報一次新空房。
    """
    now_available = set()
    newly = set()
    definitive = 0

    for row in rows:
        key = row["key"]
        # QUEUED / MAINTENANCE / BLOCKED / UNKNOWN 都代表「這次沒問到」，
        # 不能當成確定結果，否則會把還在的空房誤判成消失
        if row["status"] in ("AVAILABLE", "SOLD_OUT", "NOT_OPEN"):
            definitive += 1
            if row["status"] == "AVAILABLE":
                now_available.add(key)
                if key not in prev_available:
                    newly.add(key)
        elif key in prev_available:
            now_available.add(key)

    return now_available, newly, definitive


def price_text(row):
    return f"{row['prices'][0]:,} 円起" if row["prices"] else "價格未擷取"


def room_detail_lines(row, indent="     "):
    """有空房時列出房型、價格，以及訂房／房型介紹連結。"""
    lines = []
    offers = row.get("offers") or []
    if offers:
        for offer in sorted(offers, key=lambda o: (o["price"] is None, o["price"] or 0)):
            price = f"{offer['price']:,} 円" if offer["price"] else "價格未擷取"
            lines.append(f"{indent}🛏 {offer['room']}：{price}")
    else:
        lines.append(f"{indent}🛏 房型未擷取到（版面可能已改），請點連結確認")

    lines.append(f"{indent}🔗 直接訂房：{row['url']}")
    for label, url in room_page_links(row["hotel_cd"]):
        lines.append(f"{indent}📖 房型介紹（{label}）：{url}")
    return lines


def render_digest(rows, now, newly=frozenset()):
    """每日總覽：整個區間逐日的空房與價格。"""
    by_key = {r["key"]: r for r in rows}
    hotel_names = dict(HOTELS)
    mark = {"AVAILABLE": "⭕", "SOLD_OUT": "✖", "NOT_OPEN": "🔒",
            "QUEUED": "🚦", "MAINTENANCE": "🔧", "BLOCKED": "❔", "UNKNOWN": "❔"}

    lines = []
    unchecked = 0
    for d in target_dates():
        # 沒有資料可能是「還沒開賣所以不查」，也可能是「這一輪中途中止沒查到」，
        # 兩者意義完全不同，不能混用同一個符號。
        is_open = now >= reservation_open_at(d)
        cells = []
        for cd in MONITOR_HOTELS:
            row = by_key.get(f"{d}|{cd}")
            if row is None:
                if is_open:
                    unchecked += 1
                    cells.append(f"{cd} ⏸")
                else:
                    cells.append(f"{cd} 🔒")
            elif row["status"] == "AVAILABLE":
                cells.append(f"{cd} ⭕ {price_text(row)}")
            else:
                cells.append(f"{cd} {mark.get(row['status'], '❔')}")
        lines.append(f"{d:%m/%d}({WEEKDAY_TW[d.weekday()]})  " + "　".join(cells))

    if unchecked:
        lines.append("")
        lines.append(f"⚠️ 有 {unchecked} 筆已開賣但這一輪沒查到（掃描中途中止），"
                     "狀態未知，請以官網為準。")

    legend = ("⭕有空房  ✖客滿  🔒未開賣  ⏸本輪未查\n"
              "🚦官網排隊中  🔧系統維護  ❔讀不到")
    hotels = "　".join(f"{cd}={hotel_names.get(cd, cd)}" for cd in MONITOR_HOTELS)

    available = sorted((r for r in rows if r["status"] == "AVAILABLE"), key=lambda r: r["key"])
    if available:
        summary = [f"✅ 目前有空房的日期（{len(available)} 筆）：", ""]
        for r in available[:DIGEST_DETAIL_LIMIT]:
            flag = "🆕 " if r["key"] in newly else ""
            summary.append(f"  {flag}{r['date']:%m/%d}({WEEKDAY_TW[r['date'].weekday()]}) "
                           f"{r['hotel_name']}")
            summary.extend(room_detail_lines(r))
            summary.append("")
        rest = available[DIGEST_DETAIL_LIMIT:]
        if rest:
            summary.append(f"（另有 {len(rest)} 筆空房，詳情請見下方逐日表與官網）")
            for r in rest:
                summary.append(f"  {r['date']:%m/%d}({WEEKDAY_TW[r['date'].weekday()]}) "
                               f"{r['hotel_cd']}：{price_text(r)}")
            summary.append("")
    else:
        summary = ["目前區間內沒有任何空房。"]

    return "\n".join(lines), legend, hotels, "\n".join(summary)


def render_newly(rows, newly):
    lines = ["== 這次新出現的空房 =="]
    for row in sorted((r for r in rows if r["key"] in newly), key=lambda r: r["key"]):
        lines.append(f"  ✅ {row['date']:%m/%d}({WEEKDAY_TW[row['date'].weekday()]}) "
                     f"{row['hotel_name']}")
        lines.extend(room_detail_lines(row))
        lines.append("")
    return "\n".join(lines).rstrip()


# === Main ===

def parse_dates_arg(argv):
    """--dates 2027-01-30,2027-01-31：只查指定日期。"""
    if "--dates" not in argv:
        return None
    raw = argv[argv.index("--dates") + 1]
    return [datetime.datetime.strptime(d.strip(), "%Y-%m-%d").date()
            for d in raw.split(",") if d.strip()]


def main():
    send_line = "--no-line" not in sys.argv
    force_notify = "--force-notify" in sys.argv
    only_dates = parse_dates_arg(sys.argv)

    now = datetime.datetime.now(tz=JST)
    now_text = now.strftime("%Y-%m-%d %H:%M JST")
    today_key = now.strftime("%Y-%m-%d")

    state = load_state()
    prev_available = set(state.get("available", []))
    notified = set(state.get("notified", []))
    blocked_streak = int(state.get("blocked_streak", 0))

    if only_dates:
        # 指定日期時仍然跳過未開賣的，查了也只會看到「未開放預約」
        dates = [d for d in only_dates if now >= reservation_open_at(d)]
        skipped = [d for d in only_dates if d not in dates]
        if skipped:
            print(f"以下日期尚未開賣，略過：" +
                  "、".join(f"{d}（{reservation_open_at(d):%m/%d %H:%M} JST 開）"
                            for d in skipped))
        pending = []
    else:
        dates = open_target_dates(now)
        pending = [d for d in target_dates() if d not in dates]

    print(f"監控區間 {TARGET_START} ~ {TARGET_END}：已開賣 {len(dates)} 天、"
          f"未開賣 {len(pending)} 天；飯店 {MONITOR_HOTELS}")

    rows, newly, now_available = [], set(), set()
    definitive = 0
    if dates:
        rows = scan_dates(MONITOR_HOTELS, dates, STAY_NIGHTS_PER_SEARCH)
        now_available, newly, definitive = diff_availability(rows, prev_available)

    # --- 監控健康度：連續讀不到官網就示警一次 ---
    health_alert = None
    if dates:
        if definitive == 0:
            blocked_streak = min(blocked_streak + 1, BLOCKED_ALERT_THRESHOLD + 1)
        else:
            if blocked_streak >= BLOCKED_ALERT_THRESHOLD:
                health_alert = "✅ 監控恢復正常，已經可以正常讀取官網。"
            blocked_streak = 0
        if blocked_streak == BLOCKED_ALERT_THRESHOLD:
            health_alert = (
                f"⚠️ 監控異常：連續 {blocked_streak} 次都讀不到官網（可能被反爬蟲擋）。\n"
                "這段期間「沒收到通知」不等於「沒有空房」，請自行到官網確認。"
            )

    # --- 開賣提醒：區間內還沒開賣的日期，開賣前 3 小時提醒一次 ---
    open_alerts = []
    for d in pending:
        open_at = reservation_open_at(d)
        key = f"open|{d}"
        if key in notified:
            continue
        if open_at - now <= datetime.timedelta(hours=3):
            open_alerts.append(
                f"⏰ {fmt_date(d)} 快開賣了：{fmt_dt(open_at)}"
                f"（{humanize_delta(open_at - now)}）"
            )
            notified.add(key)

    # --- 每日總覽：每天在 DIGEST_HOUR_JST 那一輪發一次 ---
    digest_key = f"digest|{today_key}"
    digest_due = (
        bool(dates)
        and now.hour >= DIGEST_HOUR_JST
        and digest_key not in notified
    )
    if digest_due:
        notified.add(digest_key)

    # 只留最近的提醒紀錄，狀態檔才不會無限膨脹
    notified = {k for k in notified
                if not k.startswith("digest|") or k >= f"digest|{(now - datetime.timedelta(days=7)):%Y-%m-%d}"}
    if digest_due:
        notified.add(digest_key)

    # --- 組訊息 ---
    reasons = []
    if newly:
        reasons.append("有新空房")
    if digest_due:
        reasons.append("每日總覽")
    if open_alerts:
        reasons.append("開賣提醒")
    if health_alert:
        reasons.append("監控狀態")
    if force_notify:
        reasons.append("手動強制發送")

    parts = []
    if newly:
        parts.append("🚨🚨 有空房了！快去搶！\n")
        # 同一輪如果也要發總覽，總覽本身就會列出所有空房（新的標 🆕），
        # 這裡不再重複一份明細。
        if digest_due:
            parts.append(f"（本輪新增 {len(newly)} 筆空房，詳見下方總覽的 🆕 標記）")
        else:
            parts.append(render_newly(rows, newly))
        parts.append("")
    if open_alerts:
        parts.append("\n".join(open_alerts))
        parts.append("")
    if health_alert:
        parts.append(health_alert)
        parts.append("")

    parts.append(f"🏰 東京迪士尼飯店空房監控")
    parts.append(f"📅 報告時間：{now_text}")
    parts.append(f"🛏 監控區間：{TARGET_START:%Y/%m/%d} ~ {TARGET_END:%m/%d}"
                 f"（每日單晚，{ADULT_NUM} 大人 / {ROOMS_NUM} 房）")
    parts.append("━━━━━━━━━━━━━━━━━━")

    if not dates:
        soonest = min(reservation_open_at(d) for d in target_dates())
        parts.append(f"\n區間內還沒有任何日期開賣。\n"
                     f"最快是 {fmt_date(target_dates()[0])}，{humanize_delta(soonest - now)}開賣。")
    elif digest_due or force_notify:
        table, legend, hotels, summary = render_digest(rows, now, newly)
        parts.append("")
        parts.append(summary)
        parts.append("")
        parts.append("== 逐日狀況 ==")
        parts.append(table)
        parts.append("")
        parts.append(legend)
        parts.append(hotels)
        if pending:
            parts.append(f"\n🔒 還有 {len(pending)} 天未開賣"
                         f"（{pending[0]:%m/%d}~{pending[-1]:%m/%d}），每天 11:00 JST 滾動開放。")
    else:
        parts.append(f"\n已查 {len(dates)} 天 x {len(MONITOR_HOTELS)} 間，"
                     f"目前空房 {len(now_available)} 筆。")

    parts.append("")
    parts.append("━━━━━━━━━━━━━━━━━━")
    parts.append(f"🔗 官網訂房：{RESERVE_TOP}")
    parts.append("🤖 此為自動化播報服務 (GitHub Actions)")

    body = "\n".join(parts)
    print("Execution result:")
    print(body)

    # --- 寫回狀態 ---
    state["updated"] = now.isoformat(timespec="seconds")
    state["available"] = sorted(now_available)
    state["notified"] = sorted(notified)
    state["blocked_streak"] = blocked_streak
    save_state(state)
    print(f"空房 {len(now_available)} 筆、新出現 {len(newly)} 筆、"
          f"連續讀取失敗 {blocked_streak} 次")

    if not reasons:
        print("沒有新空房也沒有提醒事項，這一輪不發 LINE。")
        return

    print(f"發送 LINE，原因：{'、'.join(reasons)}")
    if not send_line:
        print("--no-line 指定，略過 LINE 廣播。")
        return

    line_token = get_line_token()
    if line_token:
        send_line_broadcast(line_token, body)
    else:
        print("Skipping LINE broadcast as tokens are not configured.")


def run_urls(argv):
    """--urls：印出可直接在手機上使用的查詢網址。

    給「手機一鍵查詢」用：HTTP Shortcuts 之類的 App 只要照這些網址
    發請求即可，不需要 Python 或瀏覽器自動化。
    """
    def arg(name, default):
        return argv[argv.index(name) + 1] if name in argv else default

    dates_raw = arg("--dates", "")
    codes = arg("--hotels", ",".join(MONITOR_HOTELS)).split(",")
    nights = int(arg("--nights", "1"))

    if dates_raw:
        dates = [datetime.datetime.strptime(d, "%Y-%m-%d").date()
                 for d in dates_raw.split(",")]
    else:
        dates = open_target_dates(datetime.datetime.now(tz=JST))

    now = datetime.datetime.now(tz=JST)
    hotel_names = dict(HOTELS)
    print(f"共 {len(dates) * len(codes)} 個網址"
          f"（{len(dates)} 個日期 x {len(codes)} 間飯店，每次 {nights} 晚，"
          f"{ADULT_NUM} 大人 / {ROOMS_NUM} 房）\n")
    for d in dates:
        open_at = reservation_open_at(d)
        state = "可查" if now >= open_at else f"未開賣（{open_at:%m/%d %H:%M} JST 開）"
        print(f"== {d} ({WEEKDAY_TW[d.weekday()]})  {state} ==")
        for cd in codes:
            print(f"  {cd} {hotel_names.get(cd, cd)}")
            print(f"  {build_search_url(cd, d, nights)}")
        print()


def needs_browser():
    """給 workflow 用：這一輪到底需不需要開瀏覽器。"""
    return bool(open_target_dates(datetime.datetime.now(tz=JST)))


if __name__ == "__main__":
    if "--urls" in sys.argv:
        run_urls(sys.argv)
    elif "--needs-browser" in sys.argv:
        print("yes" if needs_browser() else "no")
    elif "--scan" in sys.argv:
        run_scan(sys.argv)
    else:
        main()
