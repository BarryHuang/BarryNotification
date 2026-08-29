#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新竹重劃區房價報表：抓實價登錄開放資料 → 產生 docs/index.html → 有新成交就發 LINE。

資料來源：內政部不動產成交案件實際資訊資料供應系統
  https://plvr.land.moi.gov.tw/DownloadOpenData
縣市代號 J=新竹縣、O=新竹市；檔案 A=成屋買賣、B=預售屋。

用法：
  python hsinchu_housing.py               # 自動取最近 10 季，產表 + 通知
  python hsinchu_housing.py --no-notify   # 只產表
  python hsinchu_housing.py 114S1 114S2   # 指定季別
"""
import csv, io, os, re, sys, json, ssl, html, datetime, collections, statistics
import urllib.request, urllib.parse



BASE = "https://plvr.land.moi.gov.tw/DownloadSeason?season={season}&fileName={code}_lvr_land_{kind}.csv"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("LVR_CACHE") or os.path.join(HERE, ".lvr-cache")
PING = 3.305785  # 1 坪 = 3.305785 m²

# 每區：縣市代號、鄉鎮市區（None = 不限）、屋齡上限（年）、路段關鍵字
# 關鍵字由長到短比對，第一個命中者即為該筆的路段標籤；區域依 REGIONS 順序先到先得。
REGIONS = collections.OrderedDict([
    ("高鐵特區", dict(code="J", town="竹北市", max_age=5,
                   note="竹北六家，高鐵新竹站周邊", roads=[
        "光明六路東一段", "光明六路東二段", "高鐵", "嘉豐", "六家", "莊敬", "隘口",
        "十興", "文興路", "自強南路", "復興三路", "縣政九路",
    ])),
    ("光埔重劃區", dict(code="O", town=None, max_age=15,
                    note="好市多新竹店（慈雲路188號）一帶", roads=[
        "慈雲路", "埔頂一路", "埔頂二路", "埔頂三路", "埔頂路",
        "東光路", "光復路一段", "光復路二段",
    ])),
    ("關埔重劃區", dict(code="O", town=None, max_age=15,
                    note="關新路、介壽路一帶", roads=[
        "關新東路", "關新西路", "關新北路", "關新二街", "關新路",
        "介壽一路", "介壽路", "科園一路",
    ])),
    ("公道五沿線", dict(code="O", town=None, max_age=15,
                   note="公道五路／千甲路一帶（慈雲路口社區多登記為慈雲路門牌，計入光埔區）", roads=[
        "公道五路二段", "公道五路三段", "公道五路四段", "公道五路一段",
        "千甲路", "竹光路",
    ])),
])

MIN_DEALS = int(os.environ.get("MIN_DEALS", "3"))
RECENT_N = int(os.environ.get("RECENT_N", "20"))   # 每區列出最近幾筆成交明細
LO, HI = 30.0, 120.0                               # 報表價格軸範圍（萬元/坪）
TODAY = datetime.date.today().isoformat()   # 社區/建案明細至少幾筆才列出
RESIDENTIAL = ("住宅大樓", "華廈")                    # 統計單價時採計的建物型態


def roc_to_ad(s):
    s = (s or "").strip()
    if len(s) < 7 or not s.isdigit():
        return None
    return "%04d-%s-%s" % (int(s[:-4]) + 1911, s[-4:-2], s[-2:])


def house_age(built, deal):
    if not built or not deal:
        return None
    return round(((int(deal[:4]) - int(built[:4])) * 12 + int(deal[5:7]) - int(built[5:7])) / 12.0, 1)


def fetch(code, season, kind):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s_%s_%s.csv" % (code, season, kind))
    if not os.path.exists(path):
        try:
            with urllib.request.urlopen(BASE.format(season=season, code=code, kind=kind), timeout=60) as r:
                data = r.read()
        except Exception as e:
            print("  ! %s/%s/%s 下載失敗: %s" % (code, season, kind, e), file=sys.stderr)
            return []
        if len(data) < 300:
            return []
        open(path, "wb").write(data)
    rows = list(csv.DictReader(io.StringIO(open(path, "rb").read().decode("utf-8-sig", "replace"))))
    return [r for r in rows if r.get("鄉鎮市區") and not r["鄉鎮市區"].startswith("The villages")]


def classify(addr):
    """回傳 (區域, 路段) 或 (None, None)。"""
    for region, cfg in REGIONS.items():
        for road in sorted(cfg["roads"], key=len, reverse=True):
            if road in addr:
                return region, road
    return None, None


def building_of(addr):
    """門牌截到「號」，當作社區/棟別代理鍵：光明六路東二段６８５號十樓 → …６８５號"""
    m = re.match(r"^(.*?號)", addr)
    return (m.group(1) if m else addr).replace("新竹縣竹北市", "").replace("新竹市", "")


def unit_price(r):
    try:
        v = float(r.get("單價元平方公尺") or 0)
    except ValueError:
        return None
    return round(v * PING / 10000, 1) if v > 0 else None


def addr_range(names):
    """把同一建案的連續門牌收斂成「655~701號」樣式。"""
    nums = []
    for n in names:
        m = re.search(r"([0-9０-９]+)號", n)
        if m:
            nums.append(int(m.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789"))))
    if not nums:
        return list(names)[0][:22]
    lo, hi = min(nums), max(nums)
    base = re.sub(r"[0-9０-９]+號.*$", "", sorted(names)[0])
    return "%s%d號" % (base, lo) if lo == hi else "%s%d~%d號" % (base, lo, hi)


def med(vals):
    return round(statistics.median(vals), 1)


def collect_resale(seasons):
    out, skipped = [], 0
    codes = {cfg["code"] for cfg in REGIONS.values()}
    for code in sorted(codes):
        for s in seasons:
            for r in fetch(code, s, "A"):
                addr = r.get("土地位置建物門牌") or ""
                region, road = classify(addr)
                if not region:
                    continue
                cfg = REGIONS[region]
                if cfg["town"] and r.get("鄉鎮市區") != cfg["town"]:
                    continue
                try:
                    total = int(float(r.get("總價元") or 0))
                except ValueError:
                    continue
                if total <= 0:
                    continue
                deal = roc_to_ad(r.get("交易年月日"))
                if not deal or deal > TODAY:      # 原始檔偶有登打錯誤的未來日期
                    continue
                built = roc_to_ad(r.get("建築完成年月")) or ""
                age = house_age(built, deal)
                if age is None:
                    skipped += 1
                    continue
                if age > cfg["max_age"]:
                    continue
                out.append({
                    "區域": region, "季別": s, "成交日": deal, "路段": road,
                    "編號": (r.get("編號") or "").strip(),
                    "社區棟別": building_of(addr), "門牌": addr,
                    "型態": r.get("建物型態", ""), "交易標的": r.get("交易標的", ""),
                    "樓層": "%s/%s" % (r.get("移轉層次", ""), r.get("總樓層數", "")),
                    "格局": "%s房%s廳%s衛" % (r.get("建物現況格局-房", ""), r.get("建物現況格局-廳", ""), r.get("建物現況格局-衛", "")),
                    "坪數": round(float(r.get("建物移轉總面積平方公尺") or 0) / PING, 1),
                    "總價萬": round(total / 10000), "單價萬每坪": unit_price(r) or "",
                    "屋齡": age, "完工年月": built, "車位": r.get("車位類別", ""),
                    "備註": (r.get("備註") or "")[:60],
                })
    out.sort(key=lambda x: (x["區域"], x["成交日"] or ""), reverse=False)
    return out, skipped


def collect_presale(seasons):
    out = []
    codes = {cfg["code"] for cfg in REGIONS.values()}
    for code in sorted(codes):
        for s in seasons:
            for r in fetch(code, s, "B"):
                addr = r.get("土地位置建物門牌") or ""
                region, road = classify(addr)
                if not region:
                    continue
                cfg = REGIONS[region]
                if cfg["town"] and r.get("鄉鎮市區") != cfg["town"]:
                    continue
                try:
                    total = int(float(r.get("總價元") or 0))
                except ValueError:
                    continue
                deal = roc_to_ad(r.get("交易年月日"))
                if total <= 0 or (r.get("解約情形") or "").strip():
                    continue
                if not deal or deal > TODAY:
                    continue
                out.append({
                    "區域": region, "季別": s, "成交日": deal,
                    "編號": (r.get("編號") or "").strip(),
                    "路段": road, "建案名稱": r.get("建案名稱", "").strip(),
                    "棟及號": r.get("棟及號", ""), "門牌": addr,
                    "樓層": "%s/%s" % (r.get("移轉層次", ""), r.get("總樓層數", "")),
                    "坪數": round(float(r.get("建物移轉總面積平方公尺") or 0) / PING, 1),
                    "總價萬": round(total / 10000), "單價萬每坪": unit_price(r) or "",
                })
    out.sort(key=lambda x: (x["區域"], x["成交日"] or ""))
    return out


def write_csv(rows, name):
    if not rows:
        return None
    dest = os.path.join(OUT_DIR, name)
    with open(dest, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return dest



# =====================================================================
#  期間：自動取最近 N 季（民國年 + S1~S4）
# =====================================================================

NUM_SEASONS = int(os.environ.get("NUM_SEASONS", "10"))
OUT_DIR = os.path.join(HERE, "docs")
DATA_DIR = os.path.join(OUT_DIR, "data")
STATE_PATH = os.path.join(DATA_DIR, "seen.json")
SITE_URL = os.environ.get("SITE_URL", "https://barryhuang.github.io/BarryNotification/")


def recent_seasons(n=NUM_SEASONS, today=None):
    """回傳最近 n 個季別（含當季），格式如 115S2。"""
    d = today or datetime.date.today()
    y, q = d.year - 1911, (d.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append("%dS%d" % (y, q))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return list(reversed(out))


def med(v): return round(statistics.median(v), 1)
def esc(s): return html.escape(str(s))
def pos(p): return max(0.0, min(100.0, (p - LO) / (HI - LO) * 100))


def season_label(s):
    return "%d年Q%s" % (int(s[:3]), s[-1])


def bar(pmin, pmid, pmax):
    l, w = pos(pmin), max(pos(pmax) - pos(pmin), 0.8)
    return ('<span class="bar"><span class="bar-track"></span>'
            '<span class="bar-span" style="left:%.2f%%;width:%.2f%%"></span>'
            '<span class="bar-dot" style="left:%.2f%%"></span></span>' % (l, w, pos(pmid)))


def trend_svg(points):
    """points: [(季別, 中位, 筆數)] → 折線圖"""
    if len(points) < 2:
        return ""
    W, Hh, PAD = 560, 96, 18
    vals = [p[1] for p in points]
    lo, hi = min(vals) - 3, max(vals) + 3
    step = (W - PAD * 2) / (len(points) - 1)
    xy = [(PAD + i * step, Hh - PAD - (v - lo) / (hi - lo) * (Hh - PAD * 2))
          for i, (_, v, _) in enumerate(points)]
    line = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L", x, y) for i, (x, y) in enumerate(xy))
    area = line + " L%.1f,%.1f L%.1f,%.1f Z" % (xy[-1][0], Hh - PAD, xy[0][0], Hh - PAD)
    dots = "".join('<circle cx="%.1f" cy="%.1f" r="2.6" class="tdot"><title>%s 中位 %.1f 萬/坪（%d筆）</title></circle>'
                   % (x, y, season_label(points[i][0]), points[i][1], points[i][2])
                   for i, (x, y) in enumerate(xy))
    labs = ('<text x="%.1f" y="%d" class="tlab" text-anchor="start">%s</text>'
            '<text x="%.1f" y="%d" class="tlab" text-anchor="end">%s</text>'
            % (PAD, Hh - 3, season_label(points[0][0]), W - PAD, Hh - 3, season_label(points[-1][0])))
    ends = ('<text x="%.1f" y="%.1f" class="tval" text-anchor="end">%.1f</text>'
            '<text x="%.1f" y="%.1f" class="tval" text-anchor="end">%.1f</text>'
            % (xy[0][0] + 26, xy[0][1] - 7, points[0][1], W - PAD, xy[-1][1] - 7, points[-1][1]))
    return ('<svg class="trend" viewBox="0 0 %d %d" role="img" aria-label="逐季中位單價走勢">'
            '<path d="%s" class="tarea"/><path d="%s" class="tline"/>%s%s%s</svg>'
            % (W, Hh, area, line, dots, labs, ends))



def render(resale, presale, skipped, seasons):
    parts = []
    for region, cfg in REGIONS.items():
        rs = [d for d in resale if d["區域"] == region]
        priced = [d for d in rs if d["單價萬每坪"] and any(t in d["型態"] for t in RESIDENTIAL)]
        if not priced:
            continue
        ps = sorted(d["單價萬每坪"] for d in priced)
        byq = collections.defaultdict(list)
        for d in priced:
            byq[d["季別"]].append(d["單價萬每坪"])
        pts = [(s, med(byq[s]), len(byq[s])) for s in seasons if len(byq.get(s, [])) >= 3]

        roads_html = []
        by_road = collections.defaultdict(list)
        for d in priced:
            by_road[d["路段"]].append(d)
        for road, items in sorted(by_road.items(), key=lambda kv: -len(kv[1])):
            rp = sorted(d["單價萬每坪"] for d in items)
            by_proj = collections.defaultdict(list)
            for d in items:
                by_proj[d["完工年月"][:7]].append(d)
            shown = [(k, v) for k, v in by_proj.items() if len(v) >= MIN_DEALS]
            shown.sort(key=lambda kv: -med([x["單價萬每坪"] for x in kv[1]]))
            rows = []
            for k, v in shown:
                p = sorted(x["單價萬每坪"] for x in v)
                rng = addr_range([x["社區棟別"] for x in v])
                nd = len({x["社區棟別"] for x in v})
                age = max(med([x["屋齡"] for x in v]), 0)
                sizes = sorted(x["坪數"] for x in v if x["坪數"])
                rows.append(
                    '<tr><th scope="row"><span class="proj">%s</span>%s</th>'
                    '<td class="num dim col-built">%s</td><td class="num dim col-age">%.0f</td>'
                    '<td class="num dim col-size">%s</td><td class="num">%d</td>'
                    '<td class="range">%s</td><td class="num strong">%.1f</td>'
                    '<td class="num dim">%.0f–%.0f</td></tr>'
                    % (esc(rng), ('<span class="tag">%d棟</span>' % nd) if nd > 1 else "",
                       esc(k or "—"), age, ("%.0f" % med(sizes)) if sizes else "—",
                       len(v), bar(p[0], med(p), p[-1]), med(p), p[0], p[-1]))
            rest = len(items) - sum(len(v) for _, v in shown)
            foot = ('<p class="rest">另有 %d 筆散落在成交未達 %d 筆的建案</p>' % (rest, MIN_DEALS)) if rest else ""
            roads_html.append(
                '<section class="road"><header class="road-head"><h4>%s</h4>'
                '<p class="road-meta"><b>%.1f</b> 萬/坪中位 · %d 筆 · %.0f–%.0f</p></header>'
                '<div class="tw"><table><thead><tr>'
                '<th scope="col">建案（門牌範圍）</th><th scope="col" class="num col-built">完工</th>'
                '<th scope="col" class="num col-age">屋齡</th><th scope="col" class="num col-size">坪數</th>'
                '<th scope="col" class="num">筆數</th><th scope="col" class="range">單價分布（萬/坪）</th>'
                '<th scope="col" class="num">中位</th><th scope="col" class="num">區間</th>'
                '</tr></thead><tbody>%s</tbody></table></div>%s</section>'
                % (esc(road), med(rp), len(rp), rp[0], rp[-1], "".join(rows), foot))

        pre = [d for d in presale if d["區域"] == region and d["單價萬每坪"]]
        pre_html = ""
        if pre:
            byname = collections.defaultdict(list)
            for d in pre:
                byname[d["建案名稱"] or "（未填建案名）"].append(d["單價萬每坪"])
            rows = []
            for n, p in sorted(byname.items(), key=lambda kv: -med(kv[1])):
                p = sorted(p)
                rows.append('<tr><th scope="row">%s</th><td class="num">%d</td>'
                            '<td class="range">%s</td><td class="num strong">%.1f</td>'
                            '<td class="num dim">%.0f–%.0f</td></tr>'
                            % (esc(n), len(p), bar(p[0], med(p), p[-1]), med(p), p[0], p[-1]))
            pre_html = ('<section class="road presale"><header class="road-head">'
                        '<h4>預售建案</h4><p class="road-meta">%d 筆 · 建案名稱為登錄原始資料</p></header>'
                        '<div class="tw"><table><thead><tr><th scope="col">建案名稱</th>'
                        '<th scope="col" class="num">筆數</th><th scope="col" class="range">單價分布（萬/坪）</th>'
                        '<th scope="col" class="num">中位</th><th scope="col" class="num">區間</th>'
                        '</tr></thead><tbody>%s</tbody></table></div></section>'
                        % (len(pre), "".join(rows)))

        recent = sorted([d for d in rs if d["成交日"]], key=lambda d: d["成交日"], reverse=True)[:RECENT_N]
        rrows = []
        for d in recent:
            up = ("%.1f" % d["單價萬每坪"]) if d["單價萬每坪"] else "—"
            rrows.append(
                '<tr><td class="num dim">%s</td><th scope="row"><span class="proj">%s</span></th>'
                '<td class="dim col-built">%s</td><td class="num col-age">%.0f</td>'
                '<td class="num">%s</td><td class="num">%s</td>'
                '<td class="num strong">%s</td><td class="dim col-size">%s</td></tr>'
                % (esc(d["成交日"]), esc(d["社區棟別"]), esc(d["樓層"]), max(d["屋齡"], 0),
                   esc(d["坪數"]), esc(d["總價萬"]), up, esc(d["格局"])))
        recent_html = (
            '<section class="road recent"><header class="road-head"><h4>最近 %d 筆成交</h4>'
            '<p class="road-meta">依成交日排序 · 含所有建物型態</p></header>'
            '<div class="tw"><table><thead><tr><th scope="col" class="num">成交日</th>'
            '<th scope="col">門牌</th><th scope="col" class="col-built">樓層</th>'
            '<th scope="col" class="num col-age">屋齡</th><th scope="col" class="num">坪數</th>'
            '<th scope="col" class="num">總價(萬)</th><th scope="col" class="num">萬/坪</th>'
            '<th scope="col" class="col-size">格局</th></tr></thead><tbody>%s</tbody></table></div></section>'
            % (len(rrows), "".join(rrows))) if rrows else ""

        q1, q3 = ps[len(ps)//4], ps[len(ps)*3//4]
        parts.append(
            '<section class="region" id="%s"><header class="region-head">'
            '<div class="region-title"><h2>%s</h2><p class="note">%s</p></div>'
            '<dl class="stats">'
            '<div><dt>中位單價</dt><dd><b>%.1f</b><span>萬/坪</span></dd></div>'
            '<div><dt>中間五成落在</dt><dd><b>%.0f–%.0f</b><span>萬/坪</span></dd></div>'
            '<div><dt>成交筆數</dt><dd><b>%d</b><span>屋齡≤%d年</span></dd></div>'
            '<div><dt>路段</dt><dd><b>%d</b><span>條</span></dd></div>'
            '</dl></header>%s<div class="roads">%s%s</div></section>'
            % (esc(region), esc(region), esc(cfg.get("note", "")),
               med(ps), q1, q3, len(priced), cfg["max_age"], len(by_road),
               ('<div class="trend-wrap"><h3>逐季中位單價</h3>%s</div>' % trend_svg(pts)) if pts else "",
               recent_html + "".join(roads_html), pre_html))

    nav = "".join('<a href="#%s">%s</a>' % (esc(r), esc(r))
                  for r in REGIONS if any(d["區域"] == r for d in resale))
    tmpl = open(os.path.join(HERE, "hsinchu_template.html"), encoding="utf-8").read()
    updated = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    values = dict(nav=nav, body="".join(parts), total=len(resale), skipped=skipped,
                  span="%s – %s" % (season_label(seasons[0]), season_label(seasons[-1])),
                  lo=int(LO), hi=int(HI), updated=updated)
    for k, v in values.items():
        tmpl = tmpl.replace("{{%s}}" % k, str(v))
    os.makedirs(DATA_DIR, exist_ok=True)
    open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8").write(tmpl)
    return tmpl


# =====================================================================
#  新成交比對 + LINE 通知
# =====================================================================

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def load_seen():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return set(json.load(f).get("ids", []))
    except Exception:
        return set()


def save_seen(ids):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.date.today().isoformat(),
                   "ids": sorted(ids)}, f, ensure_ascii=False, indent=0)


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


def build_message(new_resale, new_presale):
    def group(rows, key):
        g = collections.defaultdict(list)
        for d in rows:
            g[(d["區域"], key(d))].append(d)
        # 依該建案最新成交日排序
        return collections.OrderedDict(
            sorted(g.items(), key=lambda kv: max(x["成交日"] for x in kv[1]), reverse=True))

    lines = ["🏠 新竹房價報表更新", ""]
    if new_resale:
        lines.append("【新成交 %d 筆】" % len(new_resale))
        g = group(new_resale, lambda d: d["社區棟別"])
        for (region, name), v in list(g.items())[:10]:
            ups = sorted(x["單價萬每坪"] for x in v if x["單價萬每坪"])
            price = ("%.0f–%.0f萬/坪" % (ups[0], ups[-1])) if len(ups) > 1 else (
                ("%.1f萬/坪" % ups[0]) if ups else "—")
            sizes = sorted(x["坪數"] for x in v if x["坪數"])
            size = ("%.0f–%.0f坪" % (sizes[0], sizes[-1])) if len(sizes) > 1 else (
                ("%.0f坪" % sizes[0]) if sizes else "")
            n = ("×%d筆 " % len(v)) if len(v) > 1 else ""
            lines.append("· %s %s｜%s %s%s %s"
                         % (max(x["成交日"] for x in v)[2:].replace("-", "/"), region, name, n, size, price))
        if len(g) > 10:
            lines.append("· …另 %d 個建案" % (len(g) - 10))
        lines.append("")
    if new_presale:
        lines.append("【新預售 %d 筆】" % len(new_presale))
        g = group(new_presale, lambda d: d["建案名稱"] or "未命名")
        for (region, name), v in list(g.items())[:8]:
            ups = sorted(x["單價萬每坪"] for x in v if x["單價萬每坪"])
            price = ("%.0f–%.0f萬/坪" % (ups[0], ups[-1])) if len(ups) > 1 else (
                ("%.1f萬/坪" % ups[0]) if ups else "—")
            n = ("×%d筆 " % len(v)) if len(v) > 1 else ""
            lines.append("· %s %s｜%s %s%s"
                         % (max(x["成交日"] for x in v)[2:].replace("-", "/"), region, name, n, price))
        if len(g) > 8:
            lines.append("· …另 %d 個建案" % (len(g) - 8))
        lines.append("")
    lines.append("完整報表 " + SITE_URL)
    return "\n".join(lines)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    notify = "--no-notify" not in sys.argv
    first_run = "--seed" in sys.argv
    seasons = args or recent_seasons()
    print("期間：%s" % ", ".join(seasons))

    resale, skipped = collect_resale(seasons)
    presale = collect_presale(seasons)
    render(resale, presale, skipped, seasons)
    write_csv(resale, "hsinchu_resale.csv")
    write_csv(presale, "hsinchu_presale.csv")
    print("成屋 %d 筆、預售 %d 筆（另 %d 筆缺完工年月）" % (len(resale), len(presale), skipped))

    seen = load_seen()
    new_resale = [d for d in resale if d["編號"] and d["編號"] not in seen]
    new_presale = [d for d in presale if d["編號"] and d["編號"] not in seen]
    new_resale.sort(key=lambda d: d["成交日"] or "", reverse=True)
    new_presale.sort(key=lambda d: d["成交日"] or "", reverse=True)
    # 只保留目前季別窗內的編號，狀態檔才不會無限膨脹
    save_seen({d["編號"] for d in resale + presale if d["編號"]})

    if not seen or first_run:
        print("首次建立比對基準，跳過通知（%d 筆納入基準）" % len(new_resale + new_presale))
        return
    if not (new_resale or new_presale):
        print("沒有新成交，不發通知。")
        return
    msg = build_message(new_resale, new_presale)
    print(msg)
    if notify:
        token = get_line_token()
        if token:
            send_line_broadcast(token, msg)


if __name__ == "__main__":
    main()
