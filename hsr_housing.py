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

# 資金試算的假設。自備款比例是最該自己改的一個：投資用的第二戶以上會受央行
# 信用管制，成數比自住低得多，實際要問銀行。利率與年限給一般水準當基準。
DOWN_PCT = float(os.environ.get("HSR_DOWN_PCT", "0.3"))   # 自備款占總價
FEE_PCT = float(os.environ.get("HSR_FEE_PCT", "0.02"))    # 契稅、代書、仲介等雜費占總價
LOAN_RATE = float(os.environ.get("HSR_LOAN_RATE", "0.025"))
LOAN_YEARS = int(os.environ.get("HSR_LOAN_YEARS", "20"))

# 長期試算：貸款還完之後才是真正看得出划不划算的地方。
COST_RATIO = float(os.environ.get("HSR_COST_RATIO", "0.25"))  # 持有成本占租金（稅、管理費、修繕、空置）
HOLD_YEARS = int(os.environ.get("HSR_HOLD_YEARS", "20"))      # 持有幾年後賣出
RENT_GROWTH = float(os.environ.get("HSR_RENT_GROWTH", "0.0"))
BENCH_RATE = float(os.environ.get("HSR_BENCH_RATE", "0.02"))  # 對照基準，預設當定存／債券
SELL_FEE_PCT = float(os.environ.get("HSR_SELL_FEE_PCT", "0.04"))  # 賣出的仲介與代書費

# 房地合一稅 2.0（境內居住者個人），按持有年數的稅率。課稅基礎是「獲利」而不是
# 售價，所以房價不漲就不用繳——這也意味著它只在這筆投資真的賺錢時才咬一口。
LAND_TAX = ((2, 0.45), (5, 0.35), (10, 0.20), (10 ** 9, 0.15))

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
    # 單價／坪 扣掉車位，那是市場慣用的報價基準，用來跟仲介的數字對照。
    # 全額萬／全坪／實付每坪 含車位，投報率要用這一組——租賃檔的面積本來就
    # 含車位（72% 有填，中位 9.4 坪），而車位租金幾乎沒人填（211 筆只有 8
    # 筆），所以沒辦法兩邊都把車位拆掉。把車位放在分子分母兩邊一起算，基準
    # 才一致；只扣買賣那邊會讓投報率低估 18~26%。
    gross_area = lvr.num(r.get("建物移轉總面積平方公尺")) / lvr.PING
    return {
        "站": station, "季": season, "日": deal, "路": road, "房": room,
        "門牌": addr[-20:], "坪": round(area, 1),
        "全坪": round(gross_area, 1),
        "總價萬": round((total - park) / 10000),
        "全額萬": round(total / 10000),
        "車位萬": round(park / 10000),
        "單價": up,
        "實付每坪": round(total / 10000 / gross_area, 1) if gross_area > 0 else None,
        "屋齡": lvr.house_age(built, deal),
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


def amortise_balance(principal, months):
    """本息均攤：回傳 (月付, 利息合計, 本金合計, 期末餘額)。"""
    i = LOAN_RATE / 12
    n = LOAN_YEARS * 12
    pay = principal / n if i <= 0 else principal * i / (1 - (1 + i) ** (-n))
    bal, interest, principal_paid = principal, 0.0, 0.0
    for _ in range(min(months, n)):
        it = bal * i
        pr = pay - it
        interest += it
        principal_paid += pr
        bal -= pr
    return pay, interest, principal_paid, max(bal, 0.0)


def amortise(principal, months=12):
    """本息均攤：回傳 (月付, 前 months 個月的利息合計, 本金合計)。"""
    pay, interest, principal_paid, _ = amortise_balance(principal, months)
    return pay, interest, principal_paid


def _irr(cashflows):
    """月現金流序列的年化 IRR。二分法，找不到變號區間就回 None。"""
    def npv(annual):
        m = (1 + annual) ** (1 / 12) - 1
        return sum(c / (1 + m) ** t for t, c in enumerate(cashflows))
    lo, hi = -0.5, 1.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(120):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def land_tax_rate(years):
    for limit, rate in LAND_TAX:
        if years <= limit:
            return rate
    return LAND_TAX[-1][1]


def net_sale_proceeds(full_wan, appreciation, years=None):
    """期末賣出的淨入袋：售價 − 仲介代書 − 房地合一稅。單位：元。

    房地合一稅課的是獲利（售價 − 取得成本 − 費用），不是售價，所以房價不漲
    就不用繳。沒有扣「土地漲價總數額」——那要查公告土地現值，這裡算不出來；
    少扣這一項會讓稅偏高，屬於保守估計。土地增值稅同樣未計。
    """
    years = HOLD_YEARS if years is None else years
    price = full_wan * 10000 * (1 + appreciation) ** years
    sell_fee = price * SELL_FEE_PCT
    cost = full_wan * 10000 * (1 + FEE_PCT)      # 取得成本含當初的契稅代書
    gain = price - cost - sell_fee
    tax = max(0.0, gain) * land_tax_rate(years)
    return price - sell_fee - tax, tax


def _lifecycle_cf(full_wan, rent_month, appreciation):
    """期初自備＋雜費、每月租金淨額減月付、期末賣出淨額。單位：元。"""
    pay, _, _ = amortise(full_wan * (1 - DOWN_PCT) * 10000, months=1)
    cf = [-full_wan * (DOWN_PCT + FEE_PCT) * 10000]
    for m in range(1, HOLD_YEARS * 12 + 1):
        rent = rent_month * (1 + RENT_GROWTH) ** ((m - 1) / 12)
        cf.append(rent * (1 - COST_RATIO) - (pay if m <= LOAN_YEARS * 12 else 0))
    # 賣出時貸款還沒繳完的話，餘額要一次清掉
    _, _, _, balance = amortise_balance(full_wan * (1 - DOWN_PCT) * 10000,
                                        min(HOLD_YEARS, LOAN_YEARS) * 12)
    proceeds, _ = net_sale_proceeds(full_wan, appreciation)
    cf[-1] += proceeds - balance
    return cf


def longrun(full_wan, rent_month):
    """貸款還完之後的全期報酬。

    「貸款會還完」確實讓現金流在繳完那年轉正，但那不等於報酬變好——還本金
    只是把現金換成資產。真正的問題是整筆投資的 IRR，而 IRR 幾乎完全被房價
    增值決定，因為租金投報率本來就低於貸款利率。所以這裡除了 0% 增值下的
    IRR，也回推「房價每年要漲多少才打平 BENCH_RATE」，那個數字才是決策點。
    """
    if not rent_month:
        return None
    base = _irr(_lifecycle_cf(full_wan, rent_month, 0.0))
    lo, hi = -0.10, 0.20
    need = None
    for _ in range(60):
        mid = (lo + hi) / 2
        r = _irr(_lifecycle_cf(full_wan, rent_month, mid))
        if r is None:
            break
        if r < BENCH_RATE:
            lo = mid
        else:
            hi = mid
    else:
        need = (lo + hi) / 2
    return {
        "IRR": round(base * 100, 2) if base is not None else None,
        "打平增值": round(need * 100, 2) if need is not None else None,
    }


def funding(full_wan, rent_month, gross_yield=None):
    """把總價換成「要準備多少錢」「每月進出多少」「自有資金賺多少」。

    月付裡的本金不是支出，是把現金換成資產，所以月現金流為負不等於虧錢。
    真正付掉的只有利息，因此另外給「月損益（扣利息）」與自有資金報酬率。
    """
    down = full_wan * DOWN_PCT
    fee = full_wan * FEE_PCT
    loan = full_wan - down
    cash_in = down + fee
    pay, interest_y1, principal_y1 = amortise(loan * 10000)
    out = {
        "全額萬": round(full_wan),
        "自備萬": round(down),
        "雜費萬": round(fee),
        "入手資金萬": round(cash_in),
        "貸款萬": round(loan),
        "月付": round(pay),
        "月利息": round(interest_y1 / 12),
        "月本金": round(principal_y1 / 12),
    }
    if gross_yield is not None:
        # 槓桿的方向只看一件事：租金投報率有沒有高過貸款利率
        out["利差"] = round(gross_yield - LOAN_RATE * 100, 2)
        out["槓桿"] = "正" if out["利差"] > 0 else "負"
    if rent_month:
        out["月現金流"] = round(rent_month - pay)
        out["月損益"] = round(rent_month - interest_y1 / 12)
        if cash_in > 0:
            roe = (rent_month * 12 - interest_y1) / (cash_in * 10000) * 100
            out["自有資金報酬"] = round(roe, 2)
    return out


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
                fulls = [d.get("全額萬", d["總價萬"]) for d in sa]
                flo, fhi = quartiles(fulls)
                parks = [d.get("車位萬", 0) for d in sa if d.get("車位萬")]
                allin = [d["實付每坪"] for d in sa if d.get("實付每坪")]
                cell.update({
                    "單價": med(ups), "單價四分位": [lo, hi],
                    "坪數": med([d["坪"] for d in sa]),
                    "全坪": med([d.get("全坪", d["坪"]) for d in sa]),
                    "實付每坪": med(allin) if allin else None,
                    "總價萬": round(statistics.median(d["總價萬"] for d in sa)),
                    "全額萬": round(statistics.median(fulls)),
                    "全額四分位": [round(flo), round(fhi)],
                    "車位萬": round(statistics.median(parks)) if parks else 0,
                    "有車位": round(100 * len(parks) / len(sa)),
                    "屋齡": med(ages) if ages else None,
                })
            if len(rc) >= MIN_N:
                cell.update({
                    "每坪租金": round(statistics.median(d["每坪租金"] for d in rc)),
                    "月租": round(statistics.median(d["月租"] for d in rc)),
                    "租賃坪數": med([d["坪"] for d in rc]),
                })
            # 投報率用含車位的基準（實付每坪 vs 含車位面積的每坪租金）
            if cell.get("實付每坪") and cell.get("每坪租金"):
                y = cell["每坪租金"] * 12 / (cell["實付每坪"] * 10000) * 100
                cell["投報率"] = round(y, 2)
                cell["回本年"] = round(100 / y)
            if cell.get("全額萬"):
                cell["資金"] = funding(cell["全額萬"], cell.get("月租"),
                                       cell.get("投報率"))
                cell["長期"] = longrun(cell["全額萬"], cell.get("月租"))
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

    lines.append("【各站行情】總價含車位｜自備%d成+雜費｜年投報" % round(DOWN_PCT * 10))
    for station, rows in stats.items():
        lines.append("◆ %s" % station)
        for room, c in rows.items():
            if not c.get("全額萬"):
                lines.append("  %d房 樣本不足（買賣 n=%d）" % (room, c["買賣筆數"]))
                continue
            fd = c["資金"]
            head = "  %d房 %s萬（%s–%s）→ 入手 %s萬" % (
                room, format(fd["全額萬"], ","),
                format(c["全額四分位"][0], ","), format(c["全額四分位"][1], ","),
                format(fd["入手資金萬"], ","))
            if c.get("投報率"):
                head += "｜投報 %.2f%%（%s槓桿）" % (c["投報率"], fd["槓桿"])
                lines.append(head)
                lines.append("       月付 %s＝利息 %s＋本金 %s"
                             % (format(fd["月付"], ","), format(fd["月利息"], ","),
                                format(fd["月本金"], ",")))
                lines.append("       租 %s − 利息 = %s%s/月　自有資金報酬 %.2f%%"
                             % (format(c["月租"], ","),
                                "+" if fd["月損益"] >= 0 else "",
                                format(fd["月損益"], ","), fd["自有資金報酬"]))
                lr = c.get("長期") or {}
                if lr.get("IRR") is not None:
                    lines.append("       %d年IRR %.2f%%（0%%增值）→ 年漲 %s 才打平 %.0f%%"
                                 % (HOLD_YEARS, lr["IRR"],
                                    ("%+.2f%%" % lr["打平增值"])
                                    if lr.get("打平增值") is not None else "？",
                                    BENCH_RATE * 100))
            else:
                lines.append(head + "｜租金樣本不足(n=%d)" % c["租賃筆數"])
    lines.append("")
    lines.append("※ 自備%d成・利率%.2f%%・貸%d年・持有%d年・持有成本抓租金%d%%"
                 % (round(DOWN_PCT * 10), LOAN_RATE * 100, LOAN_YEARS,
                    HOLD_YEARS, round(COST_RATIO * 100)))
    lines.append("※ 貸款還完只讓現金流轉正，報酬幾乎全看增值——投報率低於利率")
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


def funding_cells(c):
    """資金那一列：實付總價 → 要準備的現金 → 每月進出。"""
    fd = c.get("資金")
    if not fd:
        return '<span class="dim">資料不足</span>'
    lo, hi = c["全額四分位"]
    cells = [
        ('實付總價', '<b>%s</b> 萬' % format(fd["全額萬"], ","),
         '四分位 %s–%s' % (format(lo, ","), format(hi, ","))),
        ('含車位', '%s 萬' % format(c["車位萬"], ","), '%d%% 的物件有車位' % c["有車位"]),
        ('自備 %d 成' % round(DOWN_PCT * 10), '<b>%s</b> 萬' % format(fd["自備萬"], ","),
         '加雜費共 %s 萬' % format(fd["入手資金萬"], ",")),
        ('貸款', '%s 萬' % format(fd["貸款萬"], ","),
         '%.2f%% · %d 年' % (LOAN_RATE * 100, LOAN_YEARS)),
        ('月付', '%s 元' % format(fd["月付"], ","),
         '利息 %s + 本金 %s' % (format(fd["月利息"], ","), format(fd["月本金"], ","))),
    ]
    if fd.get("月現金流") is not None:
        cf, pl = fd["月現金流"], fd["月損益"]
        cells.append(('月現金流', '%s%s 元' % ("+" if cf >= 0 else "", format(cf, ",")),
                      '租金減月付'))
        cells.append(('月損益（扣利息）',
                      '<b class="%s">%s%s</b> 元'
                      % ("cf-neg" if pl < 0 else "cf-pos",
                         "+" if pl >= 0 else "", format(pl, ",")),
                      '本金算資產，不是支出'))
        cells.append(('自有資金報酬', '<b class="%s">%.2f%%</b>'
                      % ("cf-neg" if fd["自有資金報酬"] < 0 else "cf-pos",
                         fd["自有資金報酬"]),
                      '第一年・%s槓桿 %+.2f%%' % (fd["槓桿"], fd["利差"])))
    lr = c.get("長期") or {}
    if lr.get("IRR") is not None:
        cells.append(('%d 年 IRR' % HOLD_YEARS,
                      '<b class="%s">%.2f%%</b>'
                      % ("cf-neg" if lr["IRR"] < BENCH_RATE * 100 else "cf-pos",
                         lr["IRR"]),
                      '0%% 增值・含期末賣出'))
        if lr.get("打平增值") is not None:
            cells.append(('打平 %.0f%% 要年漲' % (BENCH_RATE * 100),
                          '<b>%+.2f%%</b>' % lr["打平增值"],
                          '低於這個數就不如定存'))
    return "".join(
        '<span class="f"><i>%s</i><span class="fv">%s</span>%s</span>'
        % (esc(k), v, ('<em>%s</em>' % esc(sub)) if sub else "")
        for k, v, sub in cells)


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
                '<tr class="frow"><td colspan="7">%s</td></tr>'
                % (room, c["單價"], lo, hi, c["坪數"], c["總價萬"],
                   rent_cell,
                   ("%s 元" % format(c["月租"], ",")) if c.get("月租") else "—",
                   yield_cell, payback,
                   ("%.0f 年" % c["屋齡"]) if c.get("屋齡") is not None else "—",
                   c["買賣筆數"], c["租賃筆數"],
                   trend or '<span class="dim">單季筆數不足</span>',
                   funding_cells(c)))

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
