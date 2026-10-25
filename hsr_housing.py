#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高鐵站周邊房價與租金投報率監控：桃園青埔、新竹六家、台中烏日。

把實價登錄的買賣檔（A）與租賃檔（C）放在一起，按 2/3/4 房分別算出
每坪單價、每坪月租，再推出年化租金報酬率與回本年數，當作投資參考。

  python hsr_housing.py               # 產表 + 通知
  python hsr_housing.py --no-notify   # 只產表
  python hsr_housing.py --rebuild     # 忽略既有 store，重抓所有季別
  python hsr_housing.py --seed        # 重建比對基準（不列新成交）

方法上有幾個關鍵決定，都是實測後定的：

1. 站區用路名關鍵字圈選，不是整個行政區。按成交量挑路名會出錯——大園區
   海方路一年有 909 筆但在靠海那側，中位單價 18 萬/坪，把它算進青埔會
   讓中位數從 45 萬掉到 19 萬。

2. 單價自己算，不用檔案的「單價元平方公尺」。車位有面積卻沒獨立計價時，
   檔案是用含車位的面積去除總價，單價低估約兩成；那種筆數直接排除。

3. 租金排除社會住宅包租代管。實測社宅 440 元/坪 vs 一般租賃 609 元/坪，
   差 38%，不排除投報率會嚴重低估。

4. 房價取近 1 年、租金取近 3 年。租賃實價登錄只涵蓋透過租賃住宅服務業
   的件數，量比買賣稀疏得多，不拉長期間會沒有樣本。
"""
import collections
import datetime
import gzip
import html
import json
import os
import statistics
import sys

import lvr_common as lvr

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "docs")
DATA_DIR = os.path.join(OUT_DIR, "data")
# gzip 後約 70KB。這個檔案提交進 repo，CI 冷啟動才不用重抓上百 MB 的季別檔
# （有 store 的話只需要抓本期檔的 6 個小檔）。每天 commit，所以要壓。
STORE_PATH = os.path.join(DATA_DIR, "hsr_store.json.gz")
SEEN_PATH = os.path.join(DATA_DIR, "hsr_seen.json")
SOURCE_PATH = os.path.join(DATA_DIR, "hsr_source.json")
SITE_URL = os.environ.get("HSR_SITE_URL",
                          "https://barryhuang.github.io/BarryNotification/hsr.html")

SALE_SEASONS = int(os.environ.get("HSR_SALE_SEASONS", "5"))    # 房價取近幾季
RENT_SEASONS = int(os.environ.get("HSR_RENT_SEASONS", "12"))   # 租金取近幾季
PRICE_WINDOW = int(os.environ.get("HSR_PRICE_WINDOW", "4"))    # 中位單價只取最近幾季
MIN_N = int(os.environ.get("HSR_MIN_N", "8"))                  # 低於此筆數不給中位數
STALE_DAYS = int(os.environ.get("HSR_STALE_DAYS", "75"))       # 超過此天數視為來源停更
ROOMS = (2, 3, 4)

# 住宅型態。店面、辦公、工廠、套房的價格與租金邏輯不同，不混進來。
SALE_TYPES = ("住宅大樓", "華廈")
RENT_TYPES = ("住宅大樓", "華廈", "公寓")

# 只收整戶出租。租賃檔裡「分租套房」「分租雅房」的房數寫的是整戶格局、面積
# 寫的是出租的那一間，每坪租金會暴衝——實測青埔高鐵北路有 227 筆標成 3~4 房
# 但面積只有 6 坪、月租 17000，把整季中位數從 696 元/坪 拉到 2125 元/坪。
# 「獨立套房」「分層出租」的標的與買賣的 2/3/4 房不可比，一併排除。
RENT_FORMS = ("整棟(戶)出租",)
MIN_PING_PER_ROOM = 5.0   # 每房至少幾坪，擋掉殘留的分租登錄

# 路名關鍵字由長到短比對。只收名稱明確屬於高鐵站區的路段——寧可漏，不可誤收。
STATIONS = collections.OrderedDict([
    ("桃園青埔", dict(
        code="H", towns=("中壢區", "大園區"), station="高鐵桃園站",
        note="青埔重劃區，領航路系統與高鐵站前一帶",
        roads=["領航北路", "領航南路", "領航東路", "領航西路",
               "高鐵站前路", "高鐵南路", "高鐵北路",
               "青商路", "青心路", "青峰路", "青雲路", "青埔"],
    )),
    ("新竹六家", dict(
        code="J", towns=("竹北市",), station="高鐵新竹站",
        note="六家／嘉豐一帶，與縣政二期相鄰",
        roads=["光明六路東一段", "光明六路東二段", "高鐵", "嘉豐",
               "六家", "莊敬", "隘口", "十興"],
    )),
    ("台中烏日", dict(
        code="B", towns=("烏日區",), station="高鐵台中站",
        note="站前高鐵東路與三榮路一帶",
        roads=["高鐵東路", "高鐵路", "高鐵一路", "高鐵二路",
               "三榮北路", "三榮路"],
    )),
])

TODAY = datetime.date.today().isoformat()


# =====================================================================
#  篩選與轉換
# =====================================================================

def in_station(r, cfg):
    """門牌是否落在這個站區。"""
    if r.get("鄉鎮市區") not in cfg["towns"]:
        return None
    addr = r.get("土地位置建物門牌") or ""
    for kw in sorted(cfg["roads"], key=len, reverse=True):
        if kw in addr:
            return kw
    return None


def rooms_of(r):
    try:
        n = int(r.get("建物現況格局-房") or 0)
    except ValueError:
        return None
    return n if n in ROOMS else None


def sale_row(r, station, cfg, season):
    if not any(t in (r.get("建物型態") or "") for t in SALE_TYPES):
        return None
    road = in_station(r, cfg)
    if not road:
        return None
    room = rooms_of(r)
    if room is None:
        return None
    deal = lvr.roc_to_ad(r.get("交易年月日"))
    if not deal or deal > TODAY:          # 原始檔偶有登打錯誤的未來日期
        return None
    up = lvr.unit_price(r)
    if up is None:
        return None
    total = lvr.num(r.get("總價元"))
    park = lvr.num(r.get("車位總價元"))
    area = (lvr.num(r.get("建物移轉總面積平方公尺"))
            - lvr.num(r.get("車位移轉總面積平方公尺"))) / lvr.PING
    built = lvr.roc_to_ad(r.get("建築完成年月")) or ""
    addr = r.get("土地位置建物門牌") or ""
    # 欄位只留報表與統計真的會用到的，store 每天 commit，能省就省
    return {
        "站": station, "季": season, "日": deal, "路": road, "房": room,
        "門牌": addr[-20:], "坪": round(area, 1),
        "總價萬": round((total - park) / 10000),
        "單價": up, "屋齡": lvr.house_age(built, deal),
        "編號": (r.get("編號") or "").strip(),
    }


def rent_row(r, station, cfg, season):
    if lvr.is_social_housing(r):
        return None
    if (r.get("出租型態") or "").strip() not in RENT_FORMS:
        return None
    if not any(t in (r.get("建物型態") or "") for t in RENT_TYPES):
        return None
    road = in_station(r, cfg)
    if not road:
        return None
    room = rooms_of(r)
    if room is None:
        return None
    day = lvr.roc_to_ad(r.get("租賃年月日"))
    if not day or day > TODAY:
        return None
    rpp = lvr.rent_per_ping(r)
    if rpp is None:
        return None
    ping = lvr.num(r.get("建物總面積平方公尺")) / lvr.PING
    if ping / room < MIN_PING_PER_ROOM:
        return None
    built = lvr.roc_to_ad(r.get("建築完成年月")) or ""
    return {
        "站": station, "季": season, "日": day, "路": road, "房": room,
        "坪": round(ping, 1),
        "月租": round(lvr.num(r.get("總額元"))),
        "每坪租金": rpp, "屋齡": lvr.house_age(built, day),
        "編號": (r.get("編號") or "").strip(),
    }


# =====================================================================
#  資料存放：只留篩選後的列，CI 才不用每天重抓上百 MB 的季別檔
# =====================================================================

def load_store():
    try:
        with gzip.open(STORE_PATH, "rt", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("covered", {}).setdefault("A", [])
    s["covered"].setdefault("C", [])
    s.setdefault("sales", [])
    s.setdefault("rents", [])
    return s


def save_store(store):
    os.makedirs(DATA_DIR, exist_ok=True)
    store["updated"] = TODAY
    # mtime 歸零，內容沒變時 gzip 的位元組才一樣，git 不會每天看到假差異
    with gzip.GzipFile(STORE_PATH, "wb", compresslevel=9, mtime=0) as gz:
        gz.write(json.dumps(store, ensure_ascii=False,
                            separators=(",", ":"), sort_keys=True).encode("utf-8"))


def dedupe(rows):
    """同編號以最後出現的為準：本期檔排在季別檔後面，修正過的版本蓋掉舊的。"""
    out, idx = [], {}
    for d in rows:
        k = d["編號"]
        if not k:
            out.append(d)
        elif k in idx:
            out[idx[k]] = d
        else:
            idx[k] = len(out)
            out.append(d)
    return out


def refresh(store, rebuild=False):
    """補齊缺的季別，再疊上本期檔。回傳 (sales, rents)。"""
    want = {"A": lvr.recent_seasons(SALE_SEASONS),
            "C": lvr.recent_seasons(RENT_SEASONS)}
    current = lvr.recent_seasons(1)[0]
    if rebuild:
        store["covered"] = {"A": [], "C": []}
        store["sales"], store["rents"] = [], []

    builder = {"A": sale_row, "C": rent_row}
    bucket = {"A": store["sales"], "C": store["rents"]}

    for kind in ("A", "C"):
        covered = set(store["covered"][kind])
        missing = [s for s in want[kind] if s not in covered]
        if missing:
            print("  %s 檔要補的季別：%s" % (kind, ", ".join(missing)))
        for season in missing:
            got_any = False
            for station, cfg in STATIONS.items():
                raw = lvr.fetch_season(cfg["code"], season, kind)
                if raw:
                    got_any = True
                for r in raw:
                    row = builder[kind](r, station, cfg, season)
                    if row:
                        bucket[kind].append(row)
            # 當季的季別檔要整季結束才生成，抓不到不代表失敗，別標記成已收錄
            if got_any and season != current:
                covered.add(season)
        store["covered"][kind] = sorted(covered)

    # 本期檔每天都抓，這是季中唯一的新資料來源
    extra = lvr.fetch_current([cfg["code"] for cfg in STATIONS.values()],
                              ("A", "C"), SOURCE_PATH)
    for (code, kind), raw in extra.items():
        for station, cfg in STATIONS.items():
            if cfg["code"] != code:
                continue
            for r in raw:
                row = builder[kind](r, station, cfg, current)
                if row:
                    bucket[kind].append(row)

    store["sales"] = [d for d in dedupe(bucket["A"]) if d["季"] in want["A"]]
    store["rents"] = [d for d in dedupe(bucket["C"]) if d["季"] in want["C"]]
    return store["sales"], store["rents"]


# =====================================================================
#  統計
# =====================================================================

def med(v):
    return round(statistics.median(v), 1)


def quartiles(v):
    v = sorted(v)
    return v[len(v) // 4], v[len(v) * 3 // 4]


def analyse(sales, rents):
    """回傳 {站: {房數: {...}}}，以及各站的整體摘要。"""
    price_seasons = set(lvr.recent_seasons(SALE_SEASONS)[-PRICE_WINDOW:])
    out = collections.OrderedDict()
    for station in STATIONS:
        rows = collections.OrderedDict()
        for room in ROOMS:
            sa = [d for d in sales
                  if d["站"] == station and d["房"] == room and d["季"] in price_seasons]
            rc = [d for d in rents if d["站"] == station and d["房"] == room]
            cell = {"買賣筆數": len(sa), "租賃筆數": len(rc)}
            if len(sa) >= MIN_N:
                ups = [d["單價"] for d in sa]
                lo, hi = quartiles(ups)
                ages = [d["屋齡"] for d in sa if d["屋齡"] is not None]
                cell.update({
                    "單價": med(ups), "單價四分位": [lo, hi],
                    "坪數": med([d["坪"] for d in sa]),
                    "總價萬": round(statistics.median(d["總價萬"] for d in sa)),
                    "屋齡": med(ages) if ages else None,
                })
            if len(rc) >= MIN_N:
                cell.update({
                    "每坪租金": round(statistics.median(d["每坪租金"] for d in rc)),
                    "月租": round(statistics.median(d["月租"] for d in rc)),
                    "租賃坪數": med([d["坪"] for d in rc]),
                })
            if cell.get("單價") and cell.get("每坪租金"):
                y = cell["每坪租金"] * 12 / (cell["單價"] * 10000) * 100
                cell["投報率"] = round(y, 2)
                cell["回本年"] = round(100 / y)
            # 逐季中位數。單季筆數少時中位數會跳動（實測竹北 3 房從 54 到 77
            # 萬/坪），把每季攤出來，讀的人才知道那個總中位數有多穩。
            byq = collections.defaultdict(list)
            for d in sales:
                if d["站"] == station and d["房"] == room:
                    byq[d["季"]].append(d["單價"])
            cell["逐季"] = [(s, med(v), len(v))
                            for s, v in sorted(byq.items()) if len(v) >= 3]
            rows[room] = cell
        out[station] = rows
    return out


# =====================================================================
#  LINE
# =====================================================================

def load_seen():
    try:
        with open(SEEN_PATH, encoding="utf-8") as f:
            return set(json.load(f).get("ids", []))
    except Exception:
        return set()


def save_seen(ids):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": TODAY, "ids": sorted(ids)}, f,
                  ensure_ascii=False, separators=(",", ":"))


def build_message(stats, new_sales, baseline, fresh):
    latest, lag = fresh
    lines = ["🚄 高鐵站房價・租金投報 %s" % datetime.date.today().strftime("%m/%d"), ""]

    if latest is None:
        lines += ["⚠️ 這次一筆資料都沒抓到，數字沿用舊檔，請檢查下載來源。", ""]
    elif lag is not None and lag > STALE_DAYS:
        lines += ["⚠️ 來源已 %d 天沒有新資料（最新 %s），"
                  "「無新成交」不代表真的沒成交。" % (lag, latest), ""]

    if baseline:
        lines += ["（首次建立比對基準，未列新成交）", ""]
    elif new_sales:
        lines.append("【新成交 %d 筆】" % len(new_sales))
        g = collections.defaultdict(list)
        for d in new_sales:
            g[(d["站"], d["房"])].append(d)
        for (station, room), v in sorted(g.items(),
                                         key=lambda kv: -max(x["日"] for x in kv[1])):
            ups = sorted(x["單價"] for x in v)
            rng = ("%.0f–%.0f" % (ups[0], ups[-1])) if len(ups) > 1 else ("%.1f" % ups[0])
            lines.append("· %s %s｜%d房 ×%d  %s萬/坪"
                         % (max(x["日"] for x in v)[5:].replace("-", "/"),
                            station, room, len(v), rng))
        lines.append("")
    else:
        lines += ["今日無新成交揭露。", ""]

    lines.append("【各站投報率】房型｜單價｜每坪租金｜年投報")
    for station, rows in stats.items():
        lines.append("◆ %s" % station)
        for room, c in rows.items():
            if c.get("投報率"):
                lines.append("  %d房 %.1f萬/坪・%d元/坪・%.2f%%（回本%d年）"
                             % (room, c["單價"], c["每坪租金"], c["投報率"], c["回本年"]))
            elif c.get("單價"):
                lines.append("  %d房 %.1f萬/坪・租金樣本不足（n=%d）"
                             % (room, c["單價"], c["租賃筆數"]))
            else:
                lines.append("  %d房 樣本不足（買賣 n=%d）" % (room, c["買賣筆數"]))
    lines.append("")
    if latest:
        lines.append("資料涵蓋到 %s（落後 %d 天）" % (latest, lag))
    lines.append("完整報表 " + SITE_URL)
    return "\n".join(lines)


# =====================================================================
#  報表
# =====================================================================

def esc(s):
    return html.escape(str(s))


def bar(pct, lo, hi):
    """投報率橫條，共用同一條軸好橫向比較。"""
    w = max(0.0, min(100.0, (pct - lo) / (hi - lo) * 100))
    return ('<span class="bar"><i style="width:%.1f%%"></i></span>'
            '<b class="barval">%.2f%%</b>' % (w, pct))


def render(stats, sales, rents, fresh):
    latest, lag = fresh
    stale = latest is None or lag > STALE_DAYS
    yields = [c["投報率"] for rows in stats.values() for c in rows.values()
              if c.get("投報率")]
    y_hi = max(yields + [2.5])

    cards = []
    for station, rows in stats.items():
        cfg = STATIONS[station]
        trs = []
        for room, c in rows.items():
            if not c.get("單價"):
                trs.append('<tr class="thin"><th scope="row">%d房</th>'
                           '<td colspan="6">買賣樣本不足（n=%d）</td></tr>'
                           % (room, c["買賣筆數"]))
                continue
            lo, hi = c["單價四分位"]
            rent_cell = ('<b>%d</b> 元<span class="dim">／坪</span>' % c["每坪租金"]
                         if c.get("每坪租金")
                         else '<span class="dim">樣本不足 n=%d</span>' % c["租賃筆數"])
            yield_cell = (bar(c["投報率"], 0, y_hi) if c.get("投報率")
                          else '<span class="dim">—</span>')
            payback = ("%d 年" % c["回本年"]) if c.get("回本年") else "—"
            trend = "".join(
                '<span class="q"><i>%s</i>%.0f<em>n%d</em></span>'
                % (esc(s[-4:].replace("S", "/Q")), v, n) for s, v, n in c["逐季"])
            trs.append(
                '<tr><th scope="row">%d房</th>'
                '<td class="num"><b>%.1f</b><span class="dim">萬/坪</span>'
                '<span class="sub">四分位 %.0f–%.0f</span></td>'
                '<td class="num">%.0f<span class="dim">坪</span>'
                '<span class="sub">總價 %d 萬</span></td>'
                '<td class="num">%s<span class="sub">月租中位 %s</span></td>'
                '<td class="yield">%s</td>'
                '<td class="num">%s</td>'
                '<td class="num dim">%s<span class="sub">買%d / 租%d</span></td></tr>'
                '<tr class="qrow"><td colspan="7"><span class="qlab">逐季單價</span>%s</td></tr>'
                % (room, c["單價"], lo, hi, c["坪數"], c["總價萬"],
                   rent_cell,
                   ("%s 元" % format(c["月租"], ",")) if c.get("月租") else "—",
                   yield_cell, payback,
                   ("%.0f 年" % c["屋齡"]) if c.get("屋齡") is not None else "—",
                   c["買賣筆數"], c["租賃筆數"],
                   trend or '<span class="dim">單季筆數不足</span>'))

        recent = sorted((d for d in sales if d["站"] == station),
                        key=lambda d: d["日"], reverse=True)[:8]
        lis = "".join(
            '<li><span class="d">%s</span><span class="n">%s</span>'
            '<span class="m">%d房 · %.0f坪 · %d萬 · <b>%.1f</b>萬/坪</span></li>'
            % (esc(d["日"][5:]), esc(d["門牌"][:20]), d["房"], d["坪"],
               d["總價萬"], d["單價"])
            for d in recent)

        cards.append(
            '<section class="station" id="%s"><header><div class="sh">'
            '<h2>%s</h2><p class="note">%s · %s</p></div></header>'
            '<div class="scroll"><table><thead><tr>'
            '<th>房型</th><th>成交單價</th><th>權狀坪數</th><th>租金</th>'
            '<th>年化投報率</th><th>回本</th><th>屋齡</th>'
            '</tr></thead><tbody>%s</tbody></table></div>'
            '<details class="recent"><summary>最近 8 筆成交</summary>'
            '<ul class="deals">%s</ul></details></section>'
            % (esc(station), esc(station), esc(cfg["station"]), esc(cfg["note"]),
               "".join(trs), lis))

    alert = ""
    if latest is None:
        alert = ('<p class="alert">這次一筆資料都沒抓到，以下數字沿用上一版，'
                 '請檢查內政部開放資料的下載來源。</p>')
    elif stale:
        alert = ('<p class="alert">來源已 <b>%d</b> 天沒有新資料（最新 %s）。'
                 '「無新成交」只代表來源沒長大。</p>' % (lag, esc(latest)))

    price_win = lvr.recent_seasons(SALE_SEASONS)[-PRICE_WINDOW:]
    rent_win = lvr.recent_seasons(RENT_SEASONS)
    tmpl = open(os.path.join(HERE, "hsr_template.html"), encoding="utf-8").read()
    values = {
        "body": "".join(cards),
        "alert": alert,
        "fresh": ('<li%s>資料涵蓋到 <b>%s</b>%s</li>'
                  % (' class="stale"' if stale else "", esc(latest or "—"),
                     esc("（落後 %d 天）" % lag) if latest else "")),
        "price_span": "%s – %s" % (lvr.season_label(price_win[0]),
                                   lvr.season_label(price_win[-1])),
        "rent_span": "%s – %s" % (lvr.season_label(rent_win[0]),
                                  lvr.season_label(rent_win[-1])),
        "n_sale": format(len(sales), ","),
        "n_rent": format(len(rents), ","),
        "updated": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
    }
    for k, v in values.items():
        tmpl = tmpl.replace("{{%s}}" % k, str(v))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "hsr.html"), "w", encoding="utf-8") as f:
        f.write(tmpl)


# =====================================================================

def main():
    notify = "--no-notify" not in sys.argv
    rebuild = "--rebuild" in sys.argv
    first_run = "--seed" in sys.argv

    print("站區：%s" % "、".join(STATIONS))
    print("房價取近 %d 季，租金取近 %d 季" % (PRICE_WINDOW, RENT_SEASONS))

    store = load_store()
    sales, rents = refresh(store, rebuild)
    print("篩選後：買賣 %d 筆、租賃 %d 筆" % (len(sales), len(rents)))

    empty = sorted({t for t, st, _, _ in lvr.FETCH_LOG if st != "ok"})
    if empty:
        print("抓不到內容的檔案 %d 個：%s%s"
              % (len(empty), ", ".join(empty[:10]),
                 " …另 %d 個" % (len(empty) - 10) if len(empty) > 10 else ""))

    fresh = lvr.freshness([d["日"] for d in sales], [d["日"] for d in rents])
    latest, lag = fresh
    if latest is None:
        print("!! 一筆資料都沒抓到，來源可能整個掛了。", file=sys.stderr)
    else:
        print("最新資料日 %s（落後 %d 天）%s"
              % (latest, lag,
                 "  ← 超過 %d 天，來源疑似停更" % STALE_DAYS if lag > STALE_DAYS else ""))

    seen = load_seen()
    baseline = first_run or not seen
    new_sales = [d for d in sales if d["編號"] and d["編號"] not in seen]
    new_sales.sort(key=lambda d: d["日"], reverse=True)
    save_seen({d["編號"] for d in sales + rents if d["編號"]})

    stats = analyse(sales, rents)
    for station, rows in stats.items():
        for room, c in rows.items():
            print("  %-8s %d房  買賣 n=%-4d 租賃 n=%-4d  %s"
                  % (station, room, c["買賣筆數"], c["租賃筆數"],
                     ("單價 %.1f 萬/坪・租金 %d 元/坪・投報 %.2f%%"
                      % (c["單價"], c["每坪租金"], c["投報率"]))
                     if c.get("投報率") else "（樣本不足）"))

    render(stats, sales, rents, fresh)
    save_store(store)

    msg = build_message(stats, new_sales, baseline, fresh)
    print("\n" + msg)
    if notify:
        token = lvr.line_token()
        if token:
            lvr.line_broadcast(token, msg)


if __name__ == "__main__":
    main()
