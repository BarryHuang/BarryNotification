#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2027 夏威夷：HNL ⇄ OGG 島間機票票價監控 + 每日美元匯率，LINE 每日回報。

行程（見 2027Nozawa/2027_夏威夷/夏威夷行程.md）：
  7/3 (六) HNL → OGG：NH186 10:35 落地 HNL，建議 13:00 後起飛、15:00 前落地
  7/8 (四) OGG → HNL：11:00 OGG 還車，約 12:00 起飛
  3 人（2 大 1 小，8 歲；美國國內線小孩一樣是全票）

資料來源：
  機票：SerpApi 的 Google Flights API（https://serpapi.com/google-flights-api）
        需要 SERPAPI_KEY。免費方案每月 100 次，這裡每天 2 次（去、回各一）。
  匯率：ExchangeRate-API 開放端點 https://open.er-api.com/v6/latest/USD（免金鑰，每日更新）
        這是市場中間價，銀行現金/即期買賣價會再差一點。

每天的結果寫進 docs/data/hawaii_flights.json，用來算「比昨天」與「監控以來最低」。

用法：
  python hawaii_flights_fx.py              # 查詢 + 存歷史 + 發 LINE
  python hawaii_flights_fx.py --no-notify  # 只印訊息，不發 LINE、不寫歷史
"""
import os, sys, json, ssl, datetime
import urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(HERE, "docs", "data", "hawaii_flights.json")
TW = datetime.timezone(datetime.timedelta(hours=8))

ADULTS, CHILDREN = 2, 1
TRIP_START = datetime.date(2027, 7, 3)

# (代號, 標題, 出發, 抵達, 日期, 偏好起飛時段, 偏好最晚落地, 說明)
LEGS = [
    ("out", "7/3 (六) HNL → OGG", "HNL", "OGG", "2027-07-03", ("13:00", "14:30"), "15:00",
     "13:00 後起飛、15:00 前落地"),
    ("back", "7/8 (四) OGG → HNL", "OGG", "HNL", "2027-07-08", ("11:30", "13:30"), None,
     "約 12:00 起飛"),
]

SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
FX_URL = "https://open.er-api.com/v6/latest/USD"

_ctx = ssl.create_default_context()


def get_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "BarryNotification/1.0"})
    with urllib.request.urlopen(req, context=_ctx, timeout=timeout) as r:
        return json.loads(r.read().decode())


# === 匯率 ===

def fetch_fx():
    d = get_json(FX_URL)
    if d.get("result") != "success":
        raise RuntimeError("FX API: %s" % d.get("error-type"))
    return {"TWD": round(d["rates"]["TWD"], 3), "JPY": round(d["rates"]["JPY"], 2),
            "updated": d.get("time_last_update_utc", "")}


# === 機票 ===

def search_leg(dep, arr, date):
    q = urllib.parse.urlencode({
        "engine": "google_flights", "departure_id": dep, "arrival_id": arr,
        "outbound_date": date, "type": 2,  # 2 = 單程
        "adults": ADULTS, "children": CHILDREN,
        "currency": "USD", "hl": "en", "gl": "us", "api_key": SERPAPI_KEY,
    })
    d = get_json("https://serpapi.com/search.json?" + q)
    if d.get("error"):
        raise RuntimeError(d["error"])
    options = []
    for o in (d.get("best_flights") or []) + (d.get("other_flights") or []):
        segs = o.get("flights") or []
        if not segs or "price" not in o:
            continue
        options.append({
            "price": o["price"],
            "dep": segs[0]["departure_airport"]["time"][-5:],
            "arr": segs[-1]["arrival_airport"]["time"][-5:],
            "flight": " / ".join(s.get("flight_number", "?") for s in segs),
            "airline": segs[0].get("airline", ""),
            "stops": len(segs) - 1,
        })
    options.sort(key=lambda o: (o["price"], o["dep"]))
    return options, d.get("price_insights") or {}


def in_window(o, window, latest_arr):
    lo, hi = window
    return (o["stops"] == 0 and lo <= o["dep"] <= hi
            and (latest_arr is None or o["arr"] <= latest_arr))


def check_flights():
    result = {}
    for key, title, dep, arr, date, window, latest_arr, note in LEGS:
        try:
            options, insights = search_leg(dep, arr, date)
        except Exception as e:
            print("%s 查詢失敗: %s" % (title, e))
            result[key] = {"error": str(e)}
            continue
        pref = [o for o in options if in_window(o, window, latest_arr)]
        result[key] = {
            "cheapest": options[0] if options else None,
            "preferred": pref[0] if pref else None,
            "count": len(options),
            "insights": {k: insights.get(k) for k in
                         ("lowest_price", "price_level", "typical_price_range")},
        }
    return result


# === 歷史 ===

def load_history():
    try:
        with open(HISTORY, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
        f.write("\n")


def prev_entry(history, today):
    older = [h for h in history if h["date"] < today]
    return older[-1] if older else None


def price_of(entry, key, field):
    try:
        return entry["flights"][key][field]["price"]
    except (KeyError, TypeError):
        return None


def lowest_seen(history, key, field):
    vals = [price_of(h, key, field) for h in history]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def diff_text(now, before, fmt="%+d"):
    if now is None or before is None:
        return ""
    if now == before:
        return "（持平）"
    return "（比昨天 %s）" % (fmt % (now - before))


# === 訊息 ===

LEVEL_ZH = {"low": "偏低 👍", "typical": "一般", "high": "偏高"}


def build_message(today, fx, flights, history):
    prev = prev_entry(history, today)
    days_left = (TRIP_START - datetime.date.fromisoformat(today)).days
    lines = ["🌺 2027 夏威夷每日回報 %s（出發倒數 %d 天）" % (today, days_left), ""]

    lines.append("💵 美元匯率")
    if fx:
        before = (prev or {}).get("fx", {}).get("TWD")
        lines.append("1 USD = %.3f TWD%s" % (
            fx["TWD"], diff_text(fx["TWD"], before, "%+.3f")))
        lines.append("1 USD = %.2f JPY" % fx["JPY"])
    else:
        lines.append("今天抓不到匯率")

    for key, title, dep, arr, date, window, latest_arr, note in LEGS:
        lines += ["", "✈️ %s（%d 大 %d 小）" % (title, ADULTS, CHILDREN)]
        r = flights.get(key) or {}
        if "error" in r:
            lines.append("查詢失敗：%s" % r["error"][:80])
            continue
        if not r.get("cheapest"):
            lines.append("查無航班（可能還沒開賣）")
            continue
        for field, label in (("preferred", "偏好時段"), ("cheapest", "全日最低")):
            o = r.get(field)
            if field == "preferred" and not o:
                lines.append("偏好時段（%s）：查無直飛" % note)
                continue
            twd = " ≈ NT$%s" % format(round(o["price"] * fx["TWD"]), ",") if fx else ""
            lines.append("%s $%d%s%s" % (
                label, o["price"], twd,
                diff_text(o["price"], price_of(prev, key, field) if prev else None, "%+d 美元")))
            lines.append("  %s %s %s→%s" % (o["airline"], o["flight"], o["dep"], o["arr"]))
        low = lowest_seen(history, key, "preferred")
        if low is not None:
            lines.append("偏好時段監控以來最低 $%d" % low)
        ins = r.get("insights") or {}
        rng = ins.get("typical_price_range")
        if ins.get("price_level") or rng:
            lines.append("Google 評估：%s%s" % (
                LEVEL_ZH.get(ins.get("price_level"), ins.get("price_level") or "—"),
                "，一般 $%d–%d" % tuple(rng) if rng and len(rng) == 2 else ""))

    lines += ["", "票價為 Google Flights 顯示的 3 人合計（美元），匯率為市場中間價"]
    return "\n".join(lines)


# === LINE ===

def get_line_token():
    cid, secret = os.environ.get("LINE_CLIENT_ID"), os.environ.get("LINE_CLIENT_SECRET")
    if not cid or not secret:
        print("Missing LINE credentials in environment variables.")
        return None
    payload = urllib.parse.urlencode({"grant_type": "client_credentials",
                                      "client_id": cid, "client_secret": secret}).encode()
    req = urllib.request.Request("https://api.line.me/v2/oauth/accessToken", data=payload,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, context=_ctx) as r:
            return json.loads(r.read().decode()).get("access_token")
    except Exception as e:
        print("Failed to get LINE token: %s" % e)
        return None


def send_line_broadcast(token, text):
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/broadcast",
        data=json.dumps({"messages": [{"type": "text", "text": text[:4900]}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % token})
    try:
        with urllib.request.urlopen(req, context=_ctx):
            print("LINE broadcast sent successfully.")
    except Exception as e:
        print("Failed to send LINE message: %s" % e)


def main():
    notify = "--no-notify" not in sys.argv
    today = datetime.datetime.now(TW).date().isoformat()

    try:
        fx = fetch_fx()
    except Exception as e:
        print("匯率查詢失敗: %s" % e)
        fx = None

    if SERPAPI_KEY:
        flights = check_flights()
    else:
        print("Missing SERPAPI_KEY; 略過機票查詢。")
        flights = {key: {"error": "尚未設定 SERPAPI_KEY"} for key, *_ in LEGS}

    history = load_history()
    entry = {"date": today, "fx": fx or {}, "flights": flights}
    # 先把今天併進歷史，「監控以來最低」才會含今天
    merged = [h for h in history if h["date"] != today] + [entry]
    msg = build_message(today, fx, flights, merged)
    print(msg)

    if not notify:
        return
    save_history(merged)
    token = get_line_token()
    if token:
        send_line_broadcast(token, msg)


if __name__ == "__main__":
    main()
