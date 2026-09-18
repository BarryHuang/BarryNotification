#!/usr/bin/env python3
"""InnoKnight（創玖科技）IoT 車位監控.

監控目標：
    https://iot.innoknight.com/#/search/devices?keyword=<keyword>

這是一個 SPA，車位狀態由前端呼叫後端 API 取得。第一階段先用 --probe 把
前端 bundle 撈下來、找出真正的 API 端點，確認格式後再接成正式監控。

用法：
    python innoknight_parking.py --probe      # 探測 API（只印 log，不發 LINE）
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
KEYWORD = os.environ.get("INNOKNIGHT_KEYWORD", "jLcQ1nLbk96Qz2g7n0FNJA==")
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


# === Main ===

def main(argv):
    if "--probe" in argv:
        probe()
        return 0

    print("正式監控模式尚未接上 API：請先跑 --probe 找出端點。")
    print(f"頁面：{PAGE_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
