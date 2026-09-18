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

def render_page(out_dir=None, settle_ms=6000):
    """開一顆 Chromium 把 SPA 載完。

    回傳 (calls, text, html)：
      calls — 所有 XHR/fetch 的網址、payload、回應原文
      text  — 渲染完成後畫面上的文字（DOM 備援解析用）
      html  — 渲染完成後的 HTML

    SPA 的資料一定是前端自己去要的，與其猜端點，不如把瀏覽器實際發出的請求
    原封不動收下來。就算 API 欄位看不懂，畫面文字也還能當第二條解析路徑。
    """
    from playwright.sync_api import sync_playwright

    calls = []
    text = ""
    html = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-gpu", "--no-sandbox"])
        page = browser.new_page(
            user_agent=BROWSER_UA,
            viewport={"width": 430, "height": 900},
            locale="zh-TW",
        )

        def on_response(resp):
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            try:
                body = resp.text()
            except Exception as e:
                body = f"(讀不到 body: {e})"
            calls.append({
                "method": req.method,
                "url": req.url,
                "status": resp.status,
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

        try:
            page.goto(PAGE_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"  goto 逾時或失敗：{type(e).__name__}: {e}（仍繼續看已載到的東西）")
        page.wait_for_timeout(settle_ms)

        final_url = page.url
        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            text = page.inner_text("body")
        except Exception as e:
            text = f"(取不到 body 文字: {e})"
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
            try:
                page.screenshot(path=os.path.join(out_dir, "page.png"), full_page=True)
            except Exception as e:
                print(f"  截圖失敗：{e}")

        browser.close()

    print(f"  最終網址：{final_url}")
    if title:
        print(f"  標題：{title}")
    return calls, text, html


def probe_browser(out_dir="probe_out"):
    print("=" * 70)
    print(f"用瀏覽器開：{PAGE_URL}")
    print("=" * 70)

    calls, text, _ = render_page(out_dir=out_dir)

    print(f"\n### 側錄到 {len(calls)} 支 XHR/fetch")
    for i, c in enumerate(calls, 1):
        print(f"\n  --- [{i}] {c['method']} {c['url']}")
        print(f"      status={c['status']}  content-type={c['resp_content_type']}")
        if c["req_headers"]:
            print(f"      req headers: {json.dumps(c['req_headers'], ensure_ascii=False)}")
        if c["post_data"]:
            print(f"      post data  : {c['post_data'][:800]}")
        body = c["body"] or ""
        print(f"      body ({len(body)} chars):")
        print("        " + body[:3000].replace("\n", "\n        "))

    print(f"\n### 渲染後的畫面文字（{len(text)} 字）")
    print("  " + text[:4000].replace("\n", "\n  "))

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
    calls, text, _ = render_page()
    print(f"  側錄到 {len(calls)} 支 XHR/fetch，畫面文字 {len(text)} 字")

    devices = extract_devices(calls, text)
    report_devices(devices)

    if not devices:
        print("判讀不到車位，這一輪不通知也不更新狀態，避免用壞掉的資料蓋掉紀錄。")
        for c in calls:
            print(f"  [debug] {c['method']} {c['url']} -> {c['status']} "
                  f"{(c.get('body') or '')[:200]}")
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
