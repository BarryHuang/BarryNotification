#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""實價登錄開放資料的共用存取層。

資料源：內政部不動產成交案件實際資訊資料供應系統
  https://plvr.land.moi.gov.tw/DownloadOpenData

檔案命名 {縣市代號}_lvr_land_{檔別}.csv
  縣市代號  B=臺中市 H=桃園市 J=新竹縣 K=苗栗縣 O=新竹市（完整對照見 COUNTY）
  檔別      A=成屋買賣  B=預售屋買賣  C=不動產租賃

兩種取法，缺一不可：
  季別歷史檔  DownloadSeason?season=115S2&fileName=...  整季結束後才生成
  本期檔      Download?fileName=...                     每月 1、11、21 日更新

季別檔在該季結束前不存在，所以季中只靠季別檔會完全抓不到新揭露；本期檔只
含最近一期，所以只靠本期檔會缺歷史。兩個都要。

注意：查無檔案時官網回的是 HTTP 200 + 一頁 HTML 錯誤頁，不是 404，所以驗證
必須看 content-type 與長度，不能只看狀態碼。
"""
import csv
import datetime
import io
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
import zipfile

SEASON_URL = "https://plvr.land.moi.gov.tw/DownloadSeason?season={season}&fileName={code}_lvr_land_{kind}.csv"
CURRENT_CSV = "https://plvr.land.moi.gov.tw/Download?fileName={code}_lvr_land_{kind}.csv"
CURRENT_ZIP = "https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"
OPENDATA_PAGE = "https://plvr.land.moi.gov.tw/DownloadOpenData"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PING = 3.305785  # 1 坪 = 3.305785 m²

COUNTY = {
    "A": "臺北市", "B": "臺中市", "C": "基隆市", "D": "臺南市", "E": "高雄市",
    "F": "新北市", "G": "宜蘭縣", "H": "桃園市", "I": "嘉義市", "J": "新竹縣",
    "K": "苗栗縣", "M": "南投縣", "N": "彰化縣", "O": "新竹市", "P": "雲林縣",
    "Q": "嘉義縣", "T": "屏東縣", "U": "花蓮縣", "V": "臺東縣", "W": "金門縣",
    "X": "澎湖縣", "Z": "連江縣",
}

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("LVR_CACHE") or os.path.join(HERE, ".lvr-cache")

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

FETCH_LOG = []   # [(來源, 狀態, 位元組, 筆數)]


# =====================================================================
#  期間
# =====================================================================

def recent_seasons(n, today=None):
    """最近 n 個季別（含當季），格式如 115S3，由舊到新。"""
    d = today or datetime.date.today()
    y, q = d.year - 1911, (d.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append("%dS%d" % (y, q))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return list(reversed(out))


def season_label(s):
    """115S3 → 115年Q3"""
    y, _, q = s.partition("S")
    return "%s年Q%s" % (y, q)


# =====================================================================
#  下載
# =====================================================================

def _get(url, timeout=90):
    """回傳 (狀態碼, content-type, bytes)；連不到時回 (None, 錯誤訊息, b"")。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
            return r.getcode(), r.headers.get("Content-Type", ""), r.read()
    except Exception as e:
        return None, str(e), b""


def looks_like_data(ctype, data):
    """查無檔案時官網回 200 + 一頁 HTML，所以狀態碼不能當判斷依據。"""
    return len(data) >= 300 and "html" not in (ctype or "").lower()


def parse_rows(data):
    """吃原始 bytes，吐掉第二列英文欄名說明後的資料列。"""
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig", "replace"))))
    return [r for r in rows
            if r.get("鄉鎮市區") and not r["鄉鎮市區"].startswith("The villages")]


def fetch_season(code, season, kind):
    """抓季別歷史檔。當季的季檔要整季結束才生成，季中抓到的是空檔（不是錯誤）。"""
    os.makedirs(CACHE, exist_ok=True)
    tag = "%s/%s/%s" % (code, season, kind)
    path = os.path.join(CACHE, "%s_%s_%s.csv" % (code, season, kind))
    if not os.path.exists(path):
        st, ctype, data = _get(SEASON_URL.format(season=season, code=code, kind=kind))
        if st is None:
            print("  ! %s 季別檔下載失敗: %s" % (tag, ctype), file=sys.stderr)
            FETCH_LOG.append((tag, "下載失敗", 0, 0))
            return []
        if not looks_like_data(ctype, data):
            print("  · %s 季別檔尚未生成（%s %d bytes）" % (tag, ctype, len(data)))
            FETCH_LOG.append((tag, "空檔", len(data), 0))
            return []
        with open(path, "wb") as f:
            f.write(data)
    rows = parse_rows(open(path, "rb").read())
    FETCH_LOG.append((tag, "ok", os.path.getsize(path), len(rows)))
    return rows


def _current_via_csv(codes, kinds):
    out = {}
    for code in codes:
        for kind in kinds:
            url = CURRENT_CSV.format(code=code, kind=kind)
            st, ctype, data = _get(url)
            if st is None:
                print("  · 本期 %s_%s 連不上: %s" % (code, kind, ctype))
                continue
            if not looks_like_data(ctype, data):
                print("  · 本期 %s_%s 回 %s %s %d bytes，不是資料檔"
                      % (code, kind, st, ctype, len(data)))
                continue
            rows = parse_rows(data)
            if rows:
                out[(code, kind)] = rows
    return out


def _current_via_zip(codes, kinds):
    """備援：本期全國 zip。壓縮檔裡的檔名是小寫（h_lvr_land_a.csv）。"""
    st, ctype, data = _get(CURRENT_ZIP, timeout=180)
    if st is None:
        print("  · 本期 zip 連不上: %s" % ctype)
        return {}
    if not looks_like_data(ctype, data) or data[:2] != b"PK":
        print("  · 本期 zip 回 %s %s %d bytes，不是 zip" % (st, ctype, len(data)))
        return {}
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        print("  · 本期 zip 解不開: %s" % e)
        return {}
    by_lower = {n.lower(): n for n in z.namelist()}
    out = {}
    for code in codes:
        for kind in kinds:
            n = by_lower.get("%s_lvr_land_%s.csv" % (code.lower(), kind.lower()))
            if not n:
                continue
            try:
                rows = parse_rows(z.read(n))
            except Exception as e:
                print("  · 本期 zip 裡的 %s 解析失敗: %s" % (n, e))
                continue
            if rows:
                out[(code, kind)] = rows
    return out


def fetch_current(codes, kinds, source_path=None):
    """抓本期檔，回傳 {(縣市代號, 檔別): 原始列}；兩種取法都失敗時回 {}。

    單縣市 csv 是主要路徑（每檔約 50~500KB），全國 zip 是備援（約 2MB）。
    成功的模式記進 source_path，下次先試它。
    """
    remembered = None
    if source_path:
        try:
            with open(source_path, encoding="utf-8") as f:
                remembered = json.load(f).get("mode")
        except Exception:
            pass

    modes = [("csv", _current_via_csv), ("zip", _current_via_zip)]
    modes.sort(key=lambda m: m[0] != remembered)

    for name, fn in modes:
        got = fn(codes, kinds)
        if not got:
            continue
        n = sum(len(v) for v in got.values())
        print("  ✓ 本期檔（%s）：%s 共 %d 筆"
              % (name, "／".join("%s_%s" % k for k in sorted(got)), n))
        FETCH_LOG.append(("本期(%s)" % name, "ok", 0, n))
        if source_path and name != remembered:
            os.makedirs(os.path.dirname(source_path), exist_ok=True)
            with open(source_path, "w", encoding="utf-8") as f:
                json.dump({"mode": name, "updated": datetime.date.today().isoformat()},
                          f, ensure_ascii=False, indent=1)
        return got

    print("  ! 本期檔抓不到，只剩季別歷史檔（季中不會更新）。", file=sys.stderr)
    FETCH_LOG.append(("本期", "全部失敗", 0, 0))
    return {}


def load(code, kind, seasons, extra=None):
    """季別歷史檔 + 本期檔，合起來回傳原始列（附帶 _季 欄位）。"""
    out = []
    for s in seasons:
        for r in fetch_season(code, s, kind):
            r["_季"] = s
            out.append(r)
    cur = recent_seasons(1)[0]
    for r in (extra or {}).get((code, kind), []):
        r["_季"] = cur
        out.append(r)
    return out


# =====================================================================
#  欄位處理
# =====================================================================

def num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def roc_to_ad(s):
    """1150815 → 2026-08-15；格式不對時回 None。"""
    s = (s or "").strip()
    if len(s) < 7 or not s.isdigit():
        return None
    try:
        return "%04d-%s-%s" % (int(s[:-4]) + 1911, s[-4:-2], s[-2:])
    except ValueError:
        return None


def house_age(built, deal):
    if not built or not deal:
        return None
    return round(((int(deal[:4]) - int(built[:4])) * 12
                  + int(deal[5:7]) - int(built[5:7])) / 12.0, 1)


def unit_price(r):
    """每坪單價（萬元）。自己算而不用檔案的「單價元平方公尺」，因為：

    車位有獨立計價時，檔案的單價已正確排除車位；但車位有面積卻沒計價時，
    檔案是用含車位的面積去除總價，單價會被低估兩成左右。那種筆數無法把
    房屋與車位的價格拆開，所以直接回 None 由呼叫端排除（實測佔 11~21%）。
    """
    building = num(r.get("建物移轉總面積平方公尺"))
    park_area = num(r.get("車位移轉總面積平方公尺"))
    park_price = num(r.get("車位總價元"))
    if park_area > 0 and park_price <= 0:
        return None
    net = building - park_area
    if net <= 0:
        return None
    v = (num(r.get("總價元")) - park_price) / net * PING / 10000
    return round(v, 1) if 5 < v < 300 else None


def rent_per_ping(r):
    """每坪月租（元）。租賃檔的總額元就是月租。"""
    area = num(r.get("建物總面積平方公尺")) / PING
    rent = num(r.get("總額元"))
    if rent <= 3000 or area <= 5:
        return None
    v = rent / area
    return round(v) if v < 3000 else None


def is_social_housing(r):
    """社會住宅包租代管的租金是補貼價，實測比一般租賃低約四成，要排除。"""
    return "社會住宅" in (r.get("租賃住宅服務") or "")


# =====================================================================
#  資料新鮮度
# =====================================================================

def freshness(*date_lists):
    """回傳 (最新日期, 距今天數)。這是判斷來源有沒有在更新的唯一可靠訊號。"""
    days = [d for lst in date_lists for d in lst if d]
    if not days:
        return None, None
    latest = max(days)
    return latest, (datetime.date.today() - datetime.date.fromisoformat(latest)).days


# =====================================================================
#  LINE
# =====================================================================

def line_token():
    cid = os.environ.get("LINE_CLIENT_ID")
    secret = os.environ.get("LINE_CLIENT_SECRET")
    if not cid or not secret:
        print("Missing LINE credentials in environment variables.")
        return None
    payload = urllib.parse.urlencode({"grant_type": "client_credentials",
                                      "client_id": cid, "client_secret": secret}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/oauth/accessToken", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, context=_ctx) as r:
            return json.loads(r.read().decode()).get("access_token")
    except Exception as e:
        print("Failed to get LINE token: %s" % e)
        return None


def line_broadcast(token, text):
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/broadcast",
        data=json.dumps({"messages": [{"type": "text", "text": text[:4900]}]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % token})
    try:
        with urllib.request.urlopen(req, context=_ctx):
            print("LINE broadcast sent successfully.")
            return True
    except Exception as e:
        print("Failed to send LINE message: %s" % e)
        return False
