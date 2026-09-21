#!/usr/bin/env python3
"""InnoKnight（創玖科技）IoT 車位監控.

監控目標：
    https://iot.innoknight.com/#/search/devices?keyword=<keyword>

這是一個 SPA，車位狀態由前端呼叫後端 API 取得。第一階段先用 --probe 把
前端 bundle 撈下來、找出真正的 API 端點，確認格式後再接成正式監控。

用法：
    python innoknight_parking.py --probe-browser  # 用瀏覽器側錄真正的 API（推薦）
    python innoknight_parking.py --probe      # 純 HTTP 探測（不需 Playwright）
    python innoknight_parking.py              # 正式監控（有空位就發 LINE）
    python innoknight_parking.py --force-notify   # 不管有沒有空位都發一則
"""
import os
import sys
import json
import re
import ssl
import gzip
import zlib
import datetime
import urllib.request
import urllib.parse
import urllib.error

# === Config ===
SITE = "https://iot.innoknight.com"
# workflow 的 keyword 欄位留空時傳進來是空字串，不是沒設，所以要用 or 擋掉
KEYWORD = os.environ.get("INNOKNIGHT_KEYWORD") or "jLcQ1nLbk96Qz2g7n0FNJA=="
PAGE_URL = f"{SITE}/#/search/devices?keyword={urllib.parse.quote(KEYWORD, safe='')}"

STATE_FILE = os.path.join("docs", "data", "parking_seen.json")

# 車位頁要登入才看得到。實測未登入時 app 讀不到 token，router 會把
# #/search/devices 收掉丟回登入頁，連一個資料請求都不會發。這裡吃一份
# 從自己瀏覽器複製出來的登入狀態，開頁前先塞回去。
SESSION_BLOB = os.environ.get("INNOKNIGHT_SESSION") or ""

# 判斷有沒有被擋在登入牆外面
LOGIN_WORDS = ("會員登入", "手機登入", "忘記密碼", "註冊", "登入",
               "login", "sign in", "log in")

LINE_CLIENT_ID = os.environ.get("LINE_CLIENT_ID")
LINE_CLIENT_SECRET = os.environ.get("LINE_CLIENT_SECRET")

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


# === HTTP ===

def http(url, data=None, headers=None, timeout=30, method=None):
    """回傳 (status, headers, text)。失敗時 status 為 0 並把錯誤放進 text。"""
    hdrs = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        hdrs.update(headers)
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            raw = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return resp.status, dict(resp.headers), raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:
            raw = repr(raw[:400])
        return e.code, dict(e.headers or {}), raw
    except Exception as e:
        return 0, {}, f"{type(e).__name__}: {e}"


# === LINE Messaging ===

def get_line_token():
    if not LINE_CLIENT_ID or not LINE_CLIENT_SECRET:
        print("Missing LINE credentials in environment variables.")
        return None
    url = "https://api.line.me/v2/oauth/accessToken"
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": LINE_CLIENT_ID,
        "client_secret": LINE_CLIENT_SECRET,
    }).encode("utf-8")
    status, _, body = http(
        url, data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status != 200:
        print(f"Failed to get LINE token: http={status} {body[:200]}")
        return None
    return json.loads(body).get("access_token")


def send_line_broadcast(token, text):
    status, _, body = http(
        "https://api.line.me/v2/bot/message/broadcast",
        data={"messages": [{"type": "text", "text": text[:4900]}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    if status == 200:
        print("LINE broadcast sent successfully.")
    else:
        print(f"Failed to send LINE message: http={status} {body[:300]}")


# === Probe：找出真正的 API ===

ASSET_RE = re.compile(r'(?:src|href)\s*=\s*["\']([^"\']+\.(?:js|css))["\']', re.I)
# JS bundle 裡的字串常值，挑出看起來像路徑或網址的
LITERAL_RE = re.compile(r'["\'`]([^"\'`\s\\]{4,160})["\'`]')
INTERESTING = re.compile(
    r'(^https?://)|(/api)|(api/)|(device)|(parking)|(space)|(slot)|(sensor)|(vacan)',
    re.I,
)


def probe():
    print("=" * 70)
    print(f"目標頁面：{PAGE_URL}")
    print(f"keyword ：{KEYWORD}")
    print("=" * 70)

    print("\n### 1. 抓首頁 HTML")
    status, headers, html = http(SITE + "/")
    print(f"  http={status}  size={len(html)}")
    for k in ("Server", "Content-Type", "Set-Cookie"):
        if headers.get(k):
            print(f"  {k}: {headers[k][:120]}")
    if status != 200:
        print(f"  取不到首頁：{html[:400]}")
        return
    print("  --- HTML 前 1200 字 ---")
    print("  " + html[:1200].replace("\n", "\n  "))

    print("\n### 2. 前端資產")
    assets = []
    for m in ASSET_RE.finditer(html):
        src = m.group(1)
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = SITE + src
        elif not src.startswith("http"):
            src = SITE + "/" + src.lstrip("./")
        if src.endswith(".js") and src not in assets:
            assets.append(src)
    for a in assets:
        print(f"  {a}")
    if not assets:
        print("  HTML 裡沒有找到 js bundle（可能是 SSR 或需要 JS 才長出來）")

    print("\n### 3. 在 bundle 裡找 API 線索")
    found = set()
    for a in assets[:12]:
        st, _, js = http(a, timeout=60)
        print(f"  --- {a}  http={st} size={len(js)}")
        if st != 200:
            continue
        for m in LITERAL_RE.finditer(js):
            lit = m.group(1)
            if INTERESTING.search(lit) and not lit.endswith((".png", ".svg", ".jpg", ".woff", ".ttf")):
                found.add(lit)
        # axios baseURL 之類的設定
        for m in re.finditer(r'(baseURL|baseUrl|VITE_[A-Z_]*API[A-Z_]*|API_BASE|apiBase)\s*[:=]\s*["\'`]([^"\'`]{0,200})', js):
            print(f"      [config] {m.group(1)} = {m.group(2)}")
    for lit in sorted(found)[:250]:
        print(f"      {lit}")
    if not found:
        print("      (沒撈到看起來像 API 的字串)")

    print("\n### 4. 試打常見端點")
    paths = [
        "/api/device/search", "/api/devices/search", "/api/device/list",
        "/api/devices", "/api/search/devices", "/api/search/device",
        "/api/v1/device/search", "/api/v1/devices", "/api/v1/search/devices",
        "/api/public/device/search", "/api/device/query",
        "/device/search", "/search/devices",
    ]
    for p in paths:
        for url in (
            f"{SITE}{p}?keyword={urllib.parse.quote(KEYWORD, safe='')}",
            f"{SITE}{p}",
        ):
            st, _, body = http(url, headers={"Referer": PAGE_URL}, timeout=20)
            head = body[:180].replace("\n", " ")
            mark = "  <<<" if st == 200 and not head.lstrip().startswith("<!") else ""
            print(f"  GET  {url[:100]:<100} http={st} {head[:120]}{mark}")
            if url.endswith(p):  # 沒帶 query 的那個順便試 POST
                st2, _, body2 = http(
                    url, data={"keyword": KEYWORD},
                    headers={"Referer": PAGE_URL}, timeout=20,
                )
                head2 = body2[:180].replace("\n", " ")
                mark2 = "  <<<" if st2 == 200 and not head2.lstrip().startswith("<!") else ""
                print(f"  POST {url[:100]:<100} http={st2} {head2[:120]}{mark2}")

    print("\n探測結束。把上面 <<< 標記的端點或 bundle 裡的 API 路徑貼回來，就能接成正式監控。")




# === 用真實瀏覽器載入頁面，並側錄它打的每一支 API ===

def parse_session(blob):
    """把 INNOKNIGHT_SESSION 這份 secret 拆成可以塞回瀏覽器的東西。

    刻意吃得寬鬆一點，因為這份東西是人從瀏覽器 console 複製出來的：

      {"key": "值", ...}                         → 整包當 localStorage
      {"localStorage": {...},
       "sessionStorage": {...},
       "cookies": "a=b; c=d" 或 [{name,value}]}  → 各自對號入座

    回傳 (local_items, session_items, cookies)。解析不出來就回三個空的，
    呼叫端自己決定要不要繼續。
    """
    if not blob.strip():
        return {}, {}, []

    try:
        data = json.loads(blob)
    except Exception as e:
        print(f"  INNOKNIGHT_SESSION 不是合法 JSON（{e}），當作沒設定。")
        return {}, {}, []

    if not isinstance(data, dict):
        print("  INNOKNIGHT_SESSION 不是物件，當作沒設定。")
        return {}, {}, []

    structured = any(k in data for k in ("localStorage", "sessionStorage", "cookies"))
    if structured:
        local = data.get("localStorage") or {}
        session = data.get("sessionStorage") or {}
        raw_cookies = data.get("cookies") or []
    else:
        local, session, raw_cookies = data, {}, []

    def as_str_map(d):
        if not isinstance(d, dict):
            return {}
        # localStorage 的值一定是字串，物件要先 dump 回去才塞得進去
        return {
            str(k): v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            for k, v in d.items()
        }

    cookies = []
    host = urllib.parse.urlparse(SITE).hostname
    if isinstance(raw_cookies, str):
        # document.cookie 的格式："a=b; c=d"
        for pair in raw_cookies.split(";"):
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            cookies.append({"name": name.strip(), "value": value.strip(),
                            "domain": host, "path": "/"})
    elif isinstance(raw_cookies, list):
        for c in raw_cookies:
            if not isinstance(c, dict) or "name" not in c:
                continue
            c = dict(c)
            c.setdefault("domain", host)
            c.setdefault("path", "/")
            cookies.append(c)

    return as_str_map(local), as_str_map(session), cookies


def looks_like_login(text, url):
    """判斷這一頁是不是被踢回登入牆。

    兩個條件都要成立才算，避免正常頁面上剛好有「登出」之類的字就誤判：
    網址沒走到查詢路由，而且畫面上出現登入頁才有的字樣。
    """
    if "search" in (url or ""):
        return False
    return any(w in (text or "") for w in LOGIN_WORDS)


def render_page(out_dir=None, settle_ms=6000):
    """開一顆 Chromium 把 SPA 載完。

    回傳 (calls, text, html, diag)：
      calls — 側錄到的網路請求（網址、payload、回應原文）
      text  — 渲染完成後畫面上的文字（DOM 備援解析用）
      html  — 渲染完成後的 HTML
      diag  — 最終網址、標題、console 訊息等診斷資訊

    導覽分兩段做。直接 goto 帶 hash 的深層網址時，實測最後會停在
    `#/`：瀏覽器不會把 fragment 送給伺服器，SPA 的 router 在 app 還沒
    開機完成前就把路由收掉了。所以先載首頁等 app 起來，再用 client-side
    routing 切到查詢路由，讓 router 自己去打 API。
    """
    from playwright.sync_api import sync_playwright

    calls = []
    text = ""
    html = ""
    diag = {"console": [], "errors": [], "nav": []}

    # 圖片字體之類的靜態資源不用收，其餘一律收——資料不一定走 xhr/fetch
    SKIP_TYPES = ("image", "font", "stylesheet", "media")

    local_items, session_items, cookies = parse_session(SESSION_BLOB)
    diag["session"] = {
        "localStorage_keys": sorted(local_items),
        "sessionStorage_keys": sorted(session_items),
        "cookie_names": sorted(c["name"] for c in cookies),
    }
    if local_items or session_items or cookies:
        print(f"  帶入登入狀態：localStorage {len(local_items)} 筆、"
              f"sessionStorage {len(session_items)} 筆、cookie {len(cookies)} 個")
    else:
        print("  沒有 INNOKNIGHT_SESSION，會以未登入狀態開頁（車位頁大概會被擋）。")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-gpu", "--no-sandbox"])
        context = browser.new_context(
            user_agent=BROWSER_UA,
            viewport={"width": 430, "height": 900},
            locale="zh-TW",
        )
        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception as e:
                print(f"  塞 cookie 失敗：{type(e).__name__}: {e}")
        if local_items or session_items:
            # 一定要在頁面自己的 script 跑之前寫進去：app 是在 created
            # 階段讀 token 的，晚一步就已經被判定沒登入了
            context.add_init_script(
                "(() => {\n"
                "  const L = __LOCAL__, S = __SESSION__;\n"
                "  try { for (const k in L) localStorage.setItem(k, L[k]); } catch (e) {}\n"
                "  try { for (const k in S) sessionStorage.setItem(k, S[k]); } catch (e) {}\n"
                "})();"
                .replace("__LOCAL__", json.dumps(local_items, ensure_ascii=False))
                .replace("__SESSION__", json.dumps(session_items, ensure_ascii=False))
            )
        page = context.new_page()

        def on_response(resp):
            req = resp.request
            if req.resource_type in SKIP_TYPES:
                return
            try:
                body = resp.text()
            except Exception as e:
                body = f"(讀不到 body: {e})"
            calls.append({
                "method": req.method,
                "url": req.url,
                "status": resp.status,
                "resource_type": req.resource_type,
                "post_data": req.post_data,
                "req_headers": {
                    k: v for k, v in req.headers.items()
                    if k.lower() in ("content-type", "authorization", "x-token",
                                     "token", "referer", "origin", "accept")
                },
                "resp_content_type": resp.headers.get("content-type", ""),
                "body": body,
            })

        page.on("response", on_response)
        page.on("console", lambda m: diag["console"].append(f"[{m.type}] {m.text}"[:500]))
        page.on("pageerror", lambda e: diag["errors"].append(str(e)[:500]))

        def settle(label, ms):
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception as e:
                print(f"  {label}: networkidle 未達成（{type(e).__name__}），繼續")
            page.wait_for_timeout(ms)
            diag["nav"].append(f"{label} -> {page.url}")
            print(f"  {label} 後網址：{page.url}")

        # 第一段：載首頁，讓 SPA 開機
        try:
            page.goto(SITE + "/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  首頁 goto 失敗：{type(e).__name__}: {e}")
        settle("載入首頁", 3000)

        # 第二段：用 client-side routing 切到查詢路由
        route = f"/search/devices?keyword={urllib.parse.quote(KEYWORD, safe='')}"
        try:
            page.evaluate("h => { window.location.hash = h; }", route)
        except Exception as e:
            print(f"  設定 hash 失敗：{type(e).__name__}: {e}")
        settle("切換路由", settle_ms)

        # router 若把我們踢回首頁，退一步再試整個深層網址（有些 app 只吃硬導覽）
        if "search" not in page.url:
            print("  路由被收掉了，改用完整深層網址硬導覽一次")
            try:
                page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"  深層 goto 失敗：{type(e).__name__}: {e}")
            settle("硬導覽", settle_ms)

        final_url = page.url
        try:
            title = page.title()
        except Exception:
            title = ""
        diag["final_url"] = final_url
        diag["title"] = title
        try:
            text = page.inner_text("body")
        except Exception as e:
            text = f"(取不到 body 文字: {e})"

        diag["auth"] = "login_wall" if looks_like_login(text, final_url) else "ok"
        if diag["auth"] == "login_wall":
            print("  ⚠️ 被擋在登入頁，沒有拿到車位資料。")
        try:
            html = page.content()
        except Exception:
            html = ""

        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "xhr.json"), "w", encoding="utf-8") as f:
                json.dump(calls, f, ensure_ascii=False, indent=2)
            with open(os.path.join(out_dir, "page.txt"), "w", encoding="utf-8") as f:
                f.write(text)
            with open(os.path.join(out_dir, "page.html"), "w", encoding="utf-8") as f:
                f.write(html)
            with open(os.path.join(out_dir, "diag.json"), "w", encoding="utf-8") as f:
                json.dump(diag, f, ensure_ascii=False, indent=2)
            try:
                page.screenshot(path=os.path.join(out_dir, "page.png"), full_page=True)
            except Exception as e:
                print(f"  截圖失敗：{e}")

        context.close()
        browser.close()

    if title:
        print(f"  標題：{title}")
    return calls, text, html, diag


def probe_browser(out_dir="probe_out"):
    print("=" * 70)
    print(f"用瀏覽器開：{PAGE_URL}")
    print("=" * 70)

    calls, text, _, diag = render_page(out_dir=out_dir)

    print(f"\n### 登入狀態")
    sess = diag.get("session", {})
    print(f"  帶入的 localStorage key：{sess.get('localStorage_keys') or '（無）'}")
    print(f"  帶入的 cookie：{sess.get('cookie_names') or '（無）'}")
    if diag.get("auth") == "login_wall":
        print("  ❌ 被擋在登入頁 —— 沒登入就看不到車位，請設定 INNOKNIGHT_SESSION")
    else:
        print("  ✅ 沒有被踢回登入頁")

    print(f"\n### 導覽過程")
    for n in diag.get("nav", []):
        print(f"  {n}")

    print(f"\n### 側錄到 {len(calls)} 個請求（已排除圖片字體等靜態資源）")
    for i, c in enumerate(calls, 1):
        print(f"\n  --- [{i}] {c['method']} {c['url']}")
        print(f"      status={c['status']}  type={c.get('resource_type')}  "
              f"content-type={c['resp_content_type']}")
        if c["req_headers"]:
            print(f"      req headers: {json.dumps(c['req_headers'], ensure_ascii=False)}")
        if c["post_data"]:
            print(f"      post data  : {c['post_data'][:800]}")
        body = c["body"] or ""
        print(f"      body ({len(body)} chars):")
        print("        " + body[:3000].replace("\n", "\n        "))

    print(f"\n### 渲染後的畫面文字（{len(text)} 字）")
    print("  " + text[:4000].replace("\n", "\n  "))

    if diag.get("console"):
        print(f"\n### 瀏覽器 console（{len(diag['console'])} 則）")
        for m in diag["console"][:40]:
            print(f"  {m}")
    if diag.get("errors"):
        print(f"\n### 頁面 JS 錯誤（{len(diag['errors'])} 則）")
        for m in diag["errors"][:20]:
            print(f"  {m}")

    print("\n### 自動判讀結果（正式監控會用這套邏輯）")
    devices = extract_devices(calls, text)
    report_devices(devices)

    print(f"\n探測結束。{out_dir}/ 下的 xhr.json / page.txt / page.html / page.png 會上傳成 artifact。")


# === 判讀車位狀態 ===

# 一筆「裝置」至少要有個看得出是誰的欄位，以及一個看得出狀態的欄位
NAME_KEYS = ("devicename", "devname", "device_name", "name", "title", "label",
             "sn", "deviceid", "device_id", "devid", "deveui", "alias", "nickname")
STATUS_KEYS = ("status", "state", "value", "occupied", "isoccupied", "occupy",
               "parkingstatus", "parking_status", "carstatus", "car_status",
               "detect", "detected", "presence", "online", "data", "lastvalue",
               "last_value", "payload")

VACANT_WORDS = ("空位", "空車位", "空閒", "空", "可用", "未使用", "無車", "沒車",
                "vacant", "free", "available", "empty", "idle", "unoccupied")
OCCUPIED_WORDS = ("已佔用", "占用", "佔用", "使用中", "有車", "已停", "已滿", "滿",
                  "occupied", "busy", "in use", "inuse", "taken", "full")


def _norm(v):
    return str(v).strip().lower()


def classify_status(device):
    """回傳 'vacant' / 'occupied' / 'unknown'，以及當初判斷依據的原始文字。

    先看文字關鍵字，再退回布林/數字慣例（多數車位感測器用 1=有車、0=沒車）。
    """
    for key, val in device.get("_status_fields", {}).items():
        text = _norm(val)
        if not text:
            continue
        # 文字關鍵字：佔用先判，因為「已佔用」也含有「用」這類字
        for w in OCCUPIED_WORDS:
            if w in text:
                return "occupied", f"{key}={val}"
        for w in VACANT_WORDS:
            if w in text:
                return "vacant", f"{key}={val}"

    for key, val in device.get("_status_fields", {}).items():
        lk = key.lower()
        if lk in ("occupied", "isoccupied", "occupy", "detected", "presence",
                  "carstatus", "car_status", "parkingstatus", "parking_status"):
            if isinstance(val, bool):
                return ("occupied" if val else "vacant"), f"{key}={val}"
            if isinstance(val, (int, float)) or _norm(val) in ("0", "1"):
                try:
                    n = float(val)
                except (TypeError, ValueError):
                    continue
                return ("occupied" if n else "vacant"), f"{key}={val}"

    return "unknown", ""


def _walk(obj, hits):
    """在任意 JSON 結構裡找出長得像「裝置」的字典。"""
    if isinstance(obj, dict):
        keys = {k.lower(): k for k in obj.keys()}
        name_key = next((keys[k] for k in NAME_KEYS if k in keys), None)
        status_fields = {keys[k]: obj[keys[k]] for k in STATUS_KEYS if k in keys}
        # data / payload 常是巢狀物件，本身不算狀態值
        scalar_status = {
            k: v for k, v in status_fields.items()
            if not isinstance(v, (dict, list))
        }
        if name_key and scalar_status:
            hits.append({
                "name": str(obj[name_key]),
                "_status_fields": scalar_status,
                "_raw": obj,
            })
        for v in obj.values():
            _walk(v, hits)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, hits)


def extract_devices(calls, page_text=""):
    """從側錄到的 API 回應裡抽出車位清單；抽不到就退回解析畫面文字。"""
    devices = []
    seen_names = set()

    for c in calls:
        body = c.get("body") or ""
        ct = (c.get("resp_content_type") or "").lower()
        if "json" not in ct and not body.lstrip().startswith(("{", "[")):
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        hits = []
        _walk(data, hits)
        for h in hits:
            if h["name"] in seen_names:
                continue
            seen_names.add(h["name"])
            status, why = classify_status(h)
            devices.append({
                "name": h["name"],
                "status": status,
                "why": why,
                "source": c["url"],
                "fields": h["_status_fields"],
            })

    if devices:
        return devices

    # 備援：API 沒認出來的話，看畫面上一行一行的文字
    for line in (page_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if any(w in low for w in OCCUPIED_WORDS):
            devices.append({"name": line, "status": "occupied", "why": "畫面文字",
                            "source": "DOM", "fields": {}})
        elif any(w in low for w in VACANT_WORDS):
            devices.append({"name": line, "status": "vacant", "why": "畫面文字",
                            "source": "DOM", "fields": {}})
    return devices


def report_devices(devices):
    if not devices:
        print("  沒有辨識出任何車位。API 欄位可能跟預期不同，請看上面的原始回應。")
        return
    for d in devices:
        icon = {"vacant": "🟢", "occupied": "🔴"}.get(d["status"], "⚪")
        fields = json.dumps(d["fields"], ensure_ascii=False) if d["fields"] else ""
        print(f"  {icon} {d['name']}  status={d['status']}  依據={d['why'] or '—'}  {fields}")
    vacant = [d for d in devices if d["status"] == "vacant"]
    unknown = [d for d in devices if d["status"] == "unknown"]
    print(f"  合計 {len(devices)} 個車位：空 {len(vacant)}、"
          f"佔用 {len(devices) - len(vacant) - len(unknown)}、無法判讀 {len(unknown)}")


# === 狀態記錄（避免同一批空位一直重複通知）===

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# === 監控 ===

def run_monitor(force_notify=False):
    now = datetime.datetime.now(datetime.timezone.utc)
    taipei = now + datetime.timedelta(hours=8)
    stamp = taipei.strftime("%Y-%m-%d %H:%M")

    print(f"檢查時間：{stamp} (台灣時間)")
    # 也把現場存成檔案上傳成 artifact，判讀出問題時才有 HTML 與截圖可看
    calls, text, _, diag = render_page(out_dir="probe_out")
    print(f"  側錄到 {len(calls)} 個請求，畫面文字 {len(text)} 字")

    if diag.get("auth") == "login_wall":
        # 登入狀態過期跟「欄位認不出來」是兩回事，要分開講，不然看 log 的人
        # 會往錯的方向修。這種情況只要換一份 secret 就好。
        print("被擋在登入頁：INNOKNIGHT_SESSION 沒設定或已經過期。")
        print("  這一輪不通知也不更新狀態。請重新複製一份登入狀態更新 secret。")
        print(f"  [page text] {text[:600]!r}")
        for m in diag.get("console", [])[:10]:
            print(f"  [console] {m}")

        state = load_state()
        # 過期只提醒一次，不然每輪都發一則很吵
        if not state.get("auth_alert"):
            state["auth_alert"] = stamp
            save_state(state)
            token = get_line_token()
            if token:
                send_line_broadcast(
                    token,
                    f"🔑 車位監控要重新登入\n"
                    f"📅 {stamp}\n"
                    f"存的登入狀態已失效，監控暫停中。\n"
                    f"請更新 GitHub secret INNOKNIGHT_SESSION 後恢復。"
                )
        else:
            print(f"  （{state['auth_alert']} 已經提醒過一次，這輪不重複發）")
        return 0

    # 登入狀態是好的，把過期旗標清掉，下次真的過期才會再提醒
    state = load_state()
    if state.get("auth_alert"):
        state.pop("auth_alert")
        save_state(state)

    devices = extract_devices(calls, text)
    report_devices(devices)

    if not devices:
        print("判讀不到車位，這一輪不通知也不更新狀態，避免用壞掉的資料蓋掉紀錄。")
        # 判讀失敗時把現場資訊全印出來，下一輪才有東西可以調整
        for n in diag.get("nav", []):
            print(f"  [nav] {n}")
        for c in calls:
            print(f"  [req] {c['method']} {c['url']} -> {c['status']} "
                  f"({c.get('resource_type')}) {(c.get('body') or '')[:300]}")
        print(f"  [page text] {text[:1500]!r}")
        for m in diag.get("console", [])[:20]:
            print(f"  [console] {m}")
        for m in diag.get("errors", [])[:10]:
            print(f"  [jserror] {m}")
        if force_notify:
            token = get_line_token()
            if token:
                send_line_broadcast(
                    token,
                    f"⚠️ 車位監控判讀失敗\n"
                    f"📅 {stamp}\n"
                    f"頁面載得到，但認不出車位欄位，請看 GitHub Actions log。\n"
                    f"🔗 {PAGE_URL}"
                )
        return 0

    vacant = sorted(d["name"] for d in devices if d["status"] == "vacant")
    state = load_state()
    prev_vacant = set(state.get("vacant", []))
    newly = [n for n in vacant if n not in prev_vacant]

    state["vacant"] = vacant
    state["checked_at"] = taipei.strftime("%Y-%m-%dT%H:%M+08:00")
    state["total"] = len(devices)
    save_state(state)

    if not vacant:
        print("目前沒有空位。")
        if not force_notify:
            return 0
    elif not newly and not force_notify:
        print(f"有 {len(vacant)} 個空位，但都是上一輪就通知過的，這輪不重複發。")
        return 0

    lines = []
    for d in devices:
        icon = {"vacant": "🟢 空位", "occupied": "🔴 已佔用"}.get(d["status"], "⚪ 未知")
        mark = "  ← 新空出來" if d["name"] in newly else ""
        lines.append(f"  {icon}　{d['name']}{mark}")

    headline = (
        f"🅿️ 有 {len(vacant)} 個空車位！" if vacant else "🅿️ 目前沒有空車位"
    )
    body = (
        f"{headline}\n"
        f"📅 {stamp}（台灣時間）\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) + "\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔗 {PAGE_URL}\n"
        f"🤖 此為自動化播報服務 (GitHub Actions)"
    )
    print("通知內容：")
    print(body)

    token = get_line_token()
    if token:
        send_line_broadcast(token, body)
    else:
        print("沒有 LINE 憑證，略過廣播。")
    return 0


# === Main ===

def main(argv):
    if "--probe-browser" in argv:
        probe_browser()
        return 0

    if "--probe" in argv:
        probe()
        return 0

    return run_monitor(force_notify="--force-notify" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
