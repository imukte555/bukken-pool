"""都内12駅 物件新着ウォッチャー
対象: 大井町/恵比寿/広尾/代官山/目黒/中目黒/五反田/武蔵小山/不動前/戸越/蒲田/京急蒲田
種別: 中古マンション・戸建・土地・賃貸
ポータル: SUUMO中心（HOMES/ノムコム/リバブル/アットホームもベストエフォート）

フィルタ:
  共通  : 駅徒歩 ≤7分・駅ごとの許容エリア内
  売買  : 面積 ≥45㎡ ・ 3000万 ≤ 価格 ≤ 1.5億
  賃貸  : 面積 ≥30㎡ ・ 8万 ≤ 管理費込み賃料 ≤ 28万
通知  : 毎日30件目安（新着が少ない日は既出から補充）、駐車場ありを最優先で並べる
"""
import base64
import json
import os
import re
import sys
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# curl_cffi: TLS指紋を本物Chromeに偽装 (Cloudflare回避用)
try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False
    print("⚠ curl_cffi未インストール、HOMES/アットホームはスキップ", file=sys.stderr)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HTTP_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
TIMEOUT = 30
SLEEP_BETWEEN = 3.0  # bot検知対策で長め
WORKERS = 5          # 駅の並列数。上げすぎるとbot検知されるので控えめ
# 詳細ページの並列数。ホストごとのゲートで同時接続は別途絞られるので、
# プール全体としてはもう少し並べてよい（500件超を5並列だと長すぎる）
DETAIL_WORKERS = int(os.environ.get("DETAIL_WORKERS", 10))
# 同一建物から取る最大部屋数。3だと同じマンションの4部屋目以降が
# 条件を満たしていても捨てられていたので広げた（件数を増やすため）。
# 同じ部屋の重複掲載は別途 間取り+賃料+面積 の一致で除外している。
MAX_ROOMS_PER_BUILDING = 8

# 優先して通知する駅（枠の半分をここに確保し、通知の先頭に置く）
PRIORITY_STATIONS = ["恵比寿", "目黒", "中目黒"]
PRIORITY_RESERVED = 15   # 30件中この数までを優先駅に確保
PRIORITY_PER_TYPE = 4    # 各種別の枠のうち優先駅に回す上限

# === フィルタ（売買） ===
WALK_MAX = 7         # 駅徒歩上限(分) ※売買（マンション/戸建/土地）
# 全駅とも徒歩7分以内（2026-08-30 shoさん指示で例外を撤廃）。
# 以前は恵比寿など高額5駅を10分まで許容していたが、7分に統一。
WALK_MAX_BY_STATION = {}

# 条件を満たす物件が極端に少ないエリア。取りこぼさないよう深くまで見る。
DEEP_SCAN_STATIONS = ("恵比寿", "広尾", "代官山", "中目黒", "目黒", "大井町")
AREA_MIN = 45.0      # 専有/建物面積 下限(㎡)
PRICE_MIN = 3000     # 3000万
# 予算オーバーでも「価格以外は条件を満たす」物件は捨てずに価格を追い続ける。
# 不況で値下がりして予算内に入ってきた瞬間を捕まえるため。
WATCH_PRICE_MAX = 25000   # 2.5億まで監視対象（売買）
WATCH_RENT_MAX = 45.0     # 45万円/月まで監視対象（賃貸）
PRICE_MAX = 12000    # 1.2億（建物込みの予算上限）
BUILT_MAX_AGE = 30
CURRENT_YEAR = 2026

# .github/workflows/daily.yml の cron '37 21 * * *' = 06:37 JST と揃える
SCHEDULE_JST = (6, 37)

# === フィルタ（賃貸） ===
RENT_MAX = 28.0      # 管理費込み上限(万円)
RENT_MIN = 8.0       # 下限(万円) 安すぎる1Rを除外
RENT_AREA_MIN = 45.0 # 賃貸の面積下限(㎡) ※売買と同じ45㎡に統一(2026-09-08)
RENT_WALK_MAX = 7    # 賃貸の駅徒歩上限(分) ※売買と同じ7分
RENT_MAX_AGE = 20    # 賃貸の築年数上限(年) ※20年未満のみ
HOUSE_MAX_AGE = 20   # 戸建の築年数上限(年) ※20年未満のみ
MANSION_MAX_AGE = 20 # 中古マンションの築年数上限(年) ※20年未満のみ

# 1日に通知する件数（目安30件。少ない日は補充、多い日は上限でカット）
TARGET_MIN_ITEMS = 30
TARGET_MAX_ITEMS = 30

# === 駅コード ===
STATIONS = {
    "大井町": {"suumo": "05480", "homes": "oimachi_00603-st",    "nomu": "ensen_tokyo/2196/2196270", "livable": "tokyo/s2196270", "athome": "oimachi-st", "rehouse": "13/2196/270", "sumaity": "oimachi", "chintai": "tokyo/000000037", "nifty": "oimachi", "housecom": "tokyo/21960270", "sumaity_rent": "tokyo/shinagawa_ku_oimachi", "cowcamo": "1378"},
    "恵比寿": {"suumo": "05050", "homes": "ebisu_00577-st",       "nomu": "ensen_tokyo/2172/2172100", "livable": "tokyo/s2172100", "athome": "ebisu-st", "rehouse": "13/2172/100", "sumaity": "ebisu", "chintai": "tokyo/000000066", "nifty": "ebisu", "housecom": "tokyo/21590470", "sumaity_rent": "tokyo/shibuya_ku_ebisu", "cowcamo": "1332"},
    "広尾":   {"suumo": "33410", "homes": "hiro_06347-st",        "nomu": "ensen_tokyo/2344/2344190", "livable": "tokyo/s2344190", "athome": "hiro-st", "rehouse": "13/2344/190", "sumaity": "hiroo", "chintai": "tokyo/000005286", "nifty": "hiroo", "housecom": "tokyo/23440190", "sumaity_rent": "tokyo/shibuya_ku_hiroo", "cowcamo": "931"},
    "代官山": {"suumo": "21850", "homes": "daikanyama_05050-st",  "nomu": "ensen_tokyo/2321/2321020", "livable": "tokyo/s2321020", "athome": "daikanyama-st", "rehouse": "13/2321/020", "sumaity": "daikanyama", "chintai": "tokyo/000005086", "nifty": "daikanyama", "housecom": "tokyo/23210020", "sumaity_rent": "tokyo/shibuya_ku_daikanyama", "cowcamo": "489"},
    "目黒":   {"suumo": "39110", "homes": "meguro_00576-st",      "nomu": "ensen_tokyo/2172/2172090", "livable": "tokyo/s2172090", "athome": "meguro-st", "rehouse": "13/2172/090", "sumaity": "meguro", "chintai": "tokyo/000000065", "nifty": "meguro", "housecom": "tokyo/21720090", "sumaity_rent": "tokyo/shinagawa_ku_meguro", "cowcamo": "1149"},
    "中目黒": {"suumo": "27580", "homes": "nakameguro_05051-st",  "nomu": "ensen_tokyo/2321/2321030", "livable": "tokyo/s2321030", "athome": "nakameguro-st", "rehouse": "13/2344/210", "sumaity": "nakameguro", "chintai": "tokyo/000005087", "nifty": "nakameguro", "housecom": "tokyo/23210030", "sumaity_rent": "tokyo/meguro_ku_nakameguro", "cowcamo": "696"},
    "五反田": {"suumo": "14970", "homes": "gotanda_00575-st",     "nomu": "ensen_tokyo/2172/2172080", "livable": "tokyo/s2172080", "athome": "gotanda-st", "rehouse": "13/2172/080", "sumaity": "gotanda", "chintai": "tokyo/000000064", "nifty": "gotanda", "housecom": "tokyo/21720080", "sumaity_rent": "tokyo/shinagawa_ku_gotanda", "cowcamo": "201"},
    "武蔵小山": {"suumo": "38730", "homes": "musashikoyama_05069-st", "nomu": "ensen_tokyo/2327/2327230", "livable": "tokyo/s2327230", "athome": "musashikoyama-st", "rehouse": "13/2327/230", "sumaity": "musashikoyama", "chintai": "tokyo/000005103", "nifty": "musashikoyama", "housecom": "tokyo/23270230", "sumaity_rent": "tokyo/shinagawa_ku_musashikoyama", "cowcamo": "1123"},
    "不動前": {"suumo": "34410", "homes": "fudomae_05068-st",     "nomu": "ensen_tokyo/2327/2327220", "livable": "tokyo/s2327220", "athome": "fudomae-st", "rehouse": "13/2327/220", "sumaity": "fudomae", "chintai": "tokyo/000005102", "nifty": "fudomae", "housecom": "tokyo/23270220", "sumaity_rent": "tokyo/shinagawa_ku_fudomae", "cowcamo": "969"},
    "戸越":   {"suumo": "26080", "homes": "togoshi_06400-st",     "nomu": "ensen_tokyo/2351/2351170", "livable": "tokyo/s2351170", "athome": "togoshi-st", "rehouse": "13/2351/170", "sumaity": "togoshi", "chintai": "tokyo/000005352", "nifty": "togoshi", "housecom": "tokyo/23510170", "sumaity_rent": "tokyo/shinagawa_ku_togoshi", "cowcamo": "642"},
    "蒲田":   {"suumo": "08940", "homes": "kamata_00605-st",      "nomu": "ensen_tokyo/2196/2196290", "livable": "tokyo/s2196290", "athome": "kamata-st", "rehouse": "13/2196/290", "sumaity": "kamata", "chintai": "tokyo/000000039", "nifty": "kamata", "housecom": "tokyo/21960290", "sumaity_rent": "tokyo/ota_ku_kamata", "cowcamo": "1755"},
    "京急蒲田": {"suumo": "13410", "homes": "keikyukamata_05144-st", "nomu": "ensen_tokyo/2331/2331120", "livable": "tokyo/s2331120", "athome": "keikyukamata-st", "rehouse": "13/2331/120", "sumaity": "keikyukamata", "chintai": "tokyo/000005182", "nifty": "keikyukamata", "housecom": "tokyo/23310120", "sumaity_rent": "tokyo/ota_ku_keikyukamata", "cowcamo": "115"},
    "泉岳寺": {"suumo": "21340", "homes": "sengakuji_05181-st",  "nomu": "ensen_tokyo/2351/2351140", "livable": "tokyo/s2351140", "athome": "sengakuji-st", "rehouse": "13/2351/140", "sumaity": "sengakuji", "chintai": "tokyo/000005172", "nifty": "sengakuji", "housecom": "tokyo/23310010", "sumaity_rent": "tokyo/minato_ku_sengakuji", "cowcamo": "464"},
    "高輪ゲートウェイ": {"suumo": "84570", "homes": "takanawagateway_10177-st", "nomu": "ensen_tokyo/2172/2172056", "livable": "tokyo/s2172056", "athome": "takanawagateway-st", "rehouse": "13/2172/056", "sumaity": "takanawagateway", "chintai": "tokyo/000020260", "nifty": "takanawagateway", "housecom": "tokyo/21720056", "sumaity_rent": "tokyo/minato_ku_takanawagateway", "cowcamo": "1802"},
    "三田":   {"suumo": "36860", "homes": "mita_06402-st",       "nomu": "ensen_tokyo/2351/2351130", "livable": "tokyo/s2351130", "athome": "mita-st", "rehouse": "13/2352/240", "sumaity": "mita", "chintai": "tokyo/000005350", "nifty": "mita", "housecom": "tokyo/23510130", "sumaity_rent": "tokyo/minato_ku_mita", "cowcamo": "1047"},
    "大門":   {"suumo": "22090", "homes": "daimon_06403-st",                  "nomu": "ensen_tokyo/2351/2351120", "livable": "tokyo/s2351120", "athome": "daimon-st", "rehouse": "13/2358/410", "sumaity": "daimon", "chintai": "tokyo/000005349", "nifty": "daimon", "housecom": "tokyo/23510120", "sumaity_rent": "tokyo/minato_ku_daimon", "cowcamo": None},
    "新橋":   {"suumo": "20110", "homes": "shimbashi_00558-st",                  "nomu": "ensen_tokyo/2351/2351110", "livable": "tokyo/s2351110", "athome": "shimbashi-st", "rehouse": "13/2172/030", "sumaity": "shimbashi", "chintai": "tokyo/000000033", "nifty": "shimbashi", "housecom": "tokyo/21020007", "sumaity_rent": "tokyo/minato_ku_shimbashi", "cowcamo": None},
    "日本橋": {"suumo": "29710", "homes": "nihombashi_06309-st",                  "nomu": "ensen_tokyo/2351/2351080", "livable": "tokyo/s2351080", "athome": "nihombashi-st", "rehouse": "13/2341/090", "sumaity": "nihombashi", "chintai": "tokyo/000005246", "nifty": "nihombashi", "housecom": "tokyo/23410090", "sumaity_rent": "tokyo/chuo_ku_nihombashi", "cowcamo": None},
    "東日本橋": {"suumo": "32170", "homes": "higashinihombashi_06405-st",                "nomu": "ensen_tokyo/2351/2351060", "livable": "tokyo/s2351060", "athome": "higashinihombashi-st", "rehouse": "13/2351/060", "sumaity": None, "chintai": "tokyo/000005347", "nifty": "higashinihombashi", "housecom": "tokyo/23510060", "sumaity_rent": None, "cowcamo": None},
    # --- 大井町・蒲田の周辺と目黒線沿い（2026-08-29 追加。コードは実URLで検証済み）---
    # --- 2026-08-31 追加。大井町/戸越/蒲田の周辺と浅草線沿い。
    #     コードはSUUMO・ノムコムとも実URLで1駅ずつ検証済み ---
    "浜松町":   {"suumo": "31160", "homes": "hamamatsucho_00572-st", "nomu": "ensen_tokyo/2196/2196240", "livable": "tokyo/s2196240", "athome": "hamamatsucho-st", "rehouse": "13/2172/040", "sumaity": "hamamatsucho", "chintai": "tokyo/000000034", "nifty": "hamamatsucho", "housecom": "tokyo/21720040", "sumaity_rent": "tokyo/minato_ku_hamamatsucho", "cowcamo": None},
    # 伊丹空港の最寄り（大阪府豊中市）
    "蛍池":   {"suumo": "35080", "pref": "osaka", "homes": None, "nomu": None, "livable": None, "athome": "hotarugaike-st", "rehouse": "27/6668/100", "sumaity": "hotarugaike", "chintai": "osaka/000006365", "nifty": "hotarugaike", "housecom": "osaka/66680100", "sumaity_rent": "osaka/toyonaka_hotarugaike", "cowcamo": None},
    "田町":     {"suumo": "23500", "homes": "tamachi_00573-st", "nomu": "ensen_tokyo/2196/2196250", "livable": "tokyo/s2196250", "athome": "tamachi-st", "rehouse": "13/2172/050", "sumaity": "tamachi", "chintai": "tokyo/000000035", "nifty": "tamachi", "housecom": "tokyo/21720050", "sumaity_rent": "tokyo/minato_ku_tamachi", "cowcamo": "551"},
}

# 駅ごとに許容する区（これ以外の区の物件は弾く）
STATION_AREAS = {
    "大井町":   ["品川区"],
    "恵比寿":   ["渋谷区", "目黒区", "港区"],
    "広尾":     ["渋谷区", "港区"],
    "代官山":   ["渋谷区", "目黒区"],
    "目黒":     ["目黒区", "品川区", "渋谷区"],
    "中目黒":   ["目黒区", "渋谷区"],
    "五反田":   ["品川区", "港区"],
    "武蔵小山": ["品川区", "目黒区"],
    "不動前":   ["品川区", "目黒区"],
    "戸越":     ["品川区"],
    "蒲田":     ["大田区"],
    "京急蒲田": ["大田区"],
    "泉岳寺":   ["港区"],
    "高輪ゲートウェイ": ["港区"],
    "三田":     ["港区"],
    "大門":     ["港区"],
    "新橋":     ["港区", "中央区"],
    "日本橋":   ["中央区"],
    "東日本橋": ["中央区", "台東区"],
    "浜松町":   ["港区"],
    "田町":     ["港区"],
    "蛍池":     ["豊中市", "池田市"],
}

OIMACHI_REJECT_PATTERNS = [
    "西大井", "西品川", "東品川", "二葉", "豊町",
    "南大井",
    "大井5丁目", "大井5-", "大井6丁目", "大井6-", "大井7丁目",
    "南品川3丁目", "南品川3-", "南品川4丁目", "南品川4-",
]


# === ユーティリティ ===

# 1ページが極端に重いサイトは既定TIMEOUTでは落ちる（実測: リハウスの駅ページは770KB）
_HOST_TIMEOUT = {"www.rehouse.co.jp": 120}


# 「ページが存在しない」ことを示す番兵。空文字(取得失敗)と区別する
_NOT_FOUND = "\x00notfound"


def fetch(url: str, impersonate: bool = False) -> str:
    """通常はrequests。impersonate=True でChrome TLS指紋偽装 (Cloudflare突破)"""
    tmo = TIMEOUT
    for h, t in _HOST_TIMEOUT.items():
        if h in url:
            tmo = t
            break
    try:
        if impersonate and HAS_CFFI:
            r = cffi_requests.get(url, impersonate="chrome120", timeout=tmo)
        else:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=tmo)
        if r.status_code == 200 and r.text:
            return r.text
        print(f"  HTTP {r.status_code} (len={len(r.text)}): {url}", file=sys.stderr)
        if r.status_code in (404, 410):
            # ページが無いだけ。再試行してもbot検知でもないので即諦める
            # (実測: スマイティの一部駅に戸建ページが無く、404を4回再試行して
            #  サイトごと300秒スキップ扱いになっていた)
            return _NOT_FOUND
    except Exception as e:
        print(f"  fetch error: {e}", file=sys.stderr)
    return ""


def _abs(u, host):
    if not u:
        return ""
    return host + u if u.startswith("/") else u


def card_image(node):
    """カード要素から物件写真のURLを取る。lazy-load属性も見る。"""
    if node is None:
        return ""
    for img in node.find_all("img"):
        # SUUMOは rel、他は data-src 等。src は 1x1 gif のことが多いので最後
        for attr in ("rel", "data-src", "data-original", "data-lazy", "data-img", "src"):
            u = img.get(attr) or ""
            if isinstance(u, list):
                u = u[0] if u else ""
            if not u or u.startswith("data:"):
                continue
            if re.search(r"(spacer|blank|noimage|no_image|logo|icon|dummy|move_\d+_\d+)", u, re.I):
                continue
            if u.endswith(".png") and "/assets/" in u:
                continue
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("/"):
                return u          # 呼び出し側でホストを足す
            return u
    return ""


def suumo_mb(area_min):
    """SUUMOの面積下限(mb)は10刻みしか受け付けない。
    45 のような値を渡すとエラーページが返り、取得が丸ごと0件になる
    （2026-09-08に中古マンションが全駅0件になった原因）。
    取りこぼさないよう「下限を超えない最大の10刻み」に丸める。
    """
    v = int(area_min) // 10 * 10
    return max(10, v)


def parse_price_man(text: str):
    # ノムコム/リバブルは "1 億 4,280 万円" のように空白あり
    m = re.search(r"(\d+)\s*億\s*([\d,]+)?\s*万", text)
    if m:
        oku = int(m.group(1))
        man = int(m.group(2).replace(",", "")) if m.group(2) else 0
        return oku * 10000 + man
    m = re.search(r"([\d,]+)\s*万円", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def parse_area(text: str):
    # "45.91m 2" (BSがsup展開で空白入る) / "45.91m2" / "45.91㎡" / "45.91 平米"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m\s*2|m²|㎡|平米)", text)
    return float(m.group(1)) if m else None


def parse_layout(text: str):
    m = re.search(r"\b([1-9](?:S?LDK|LDK|DK|K|R)\+?[NS]?)\b", text)
    return m.group(1) if m else ""


def parse_floor(text: str, kind: str = "mansion"):
    """所在階を返す。「6階/RC9階建」→「6階」、「B1階」→「B1階」。
    ○階建（建物の高さ）だけの表記は所在階ではないので採らない。
    戸建は所在階の概念がないので階建（例「2階建」）をそのまま返す。
    土地は階がないので「—」。
    """
    if kind == "land":
        return "—"
    if not text:
        return ""
    t = text.replace("　", " ").translate(
        str.maketrans("０１２３４５６７８９", "0123456789"))
    if kind == "house":
        m = re.search(r"(地上)?(\d+)\s*階建", t)
        return f"{m.group(2)}階建" if m else ""
    # 建物側の表記（構造・階建）を先に消す。順番が逆だと「SRC13階建 8階」の
    # 8階まで構造欄と誤認して消えるので、構造 → 階建 の順で消す。
    t = re.sub(r"(造|鉄筋|鉄骨|コンクリート|ブロック|SRC|RC)\s*(地上)?\s*\d+\s*階", " ", t)
    t = re.sub(r"(地上|地下)?\s*B?\d+\s*階建(て)?", " ", t)
    m = re.search(r"(地下\s*\d+|B\s*\d+|\d+)\s*階", t)
    if not m:
        return ""
    v = m.group(1).replace(" ", "")
    if v.startswith("地下"):
        return "B" + v[2:] + "階"
    return v + "階"


def parse_walk(text: str, station: str = None):
    """駅徒歩(分)を返す。
    station指定時はその駅の徒歩のみ。指定なしの場合は
    「駅名」徒歩X分 のような駅名直後パターンのみ採用。
    PRテキストの「○○まで徒歩X分」は除外。
    """
    # 駅指定: 駅名直後の徒歩X分のみ
    if station:
        s = re.escape(station)
        # 駅名の直前が駅名の一部になる文字だと別の駅を拾ってしまう。
        # 実測: 「中目黒駅 徒歩5分」を目黒として、
        #       「東日本橋駅 徒歩2分」を日本橋として誤って採っていた。
        # 直前は「行頭 or 記号・空白・線・ヶ所の区切り」でなければならない。
        head = r"(?:^|[\s/|｜、,，。\(\)（）\[\]【】＞>・線駅分「」]|>)"
        patterns = [
            rf"「{s}」駅?\s*徒?歩\s*(\d+)\s*分",           # 「大井町」駅 徒歩4分 / 「大井町」徒歩4分
            rf"「{s}駅」\s*徒?歩\s*(\d+)\s*分",            # 「大井町駅」徒歩4分
            rf"{head}{s}駅\s*徒?歩\s*(\d+)\s*分",           # 大井町駅 徒歩4分
            rf"{head}{s}\s*駅?\s*[\s]?徒?歩\s*(\d+)\s*分",  # 大井町 徒歩4分
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return int(m.group(1))
        return None
    # 駅未指定: 「駅名」または駅名駅 直後の徒歩のみ採用
    m = re.search(r"「[^」]+」\s*徒?歩\s*(\d+)\s*分", text)
    if m:
        return int(m.group(1))
    m = re.search(r"[ァ-ヶー一-龯a-zA-Z]+駅\s*徒?歩\s*(\d+)\s*分", text)
    return int(m.group(1)) if m else None


# 住所として認める市区（誤検出を防ぐため明示列挙）。
# STATION_AREAS の全値 + 隣接23区 + 蛍池周辺(大阪)。
_ADDR_MUNI = (
    "千代田区|中央区|港区|新宿区|文京区|台東区|墨田区|江東区|品川区|目黒区|"
    "大田区|世田谷区|渋谷区|中野区|杉並区|豊島区|北区|荒川区|板橋区|練馬区|"
    "足立区|葛飾区|江戸川区|"
    "豊中市|池田市|吹田市|箕面市|大阪市[^\\s]{0,3}区|川西市|伊丹市"
)


def parse_addr(text: str):
    """住所（市区＋町丁）を返す。ここが空だと駅ごとの区チェックが効かない。"""
    m = re.search(r"((?:" + _ADDR_MUNI + r")[^\s　<>「」,、/｜|]*)", text)
    return m.group(1) if m else ""


def parse_built(text: str):
    """築年(西暦)を返す。SUUMO/ノムコム/リバブル/HOMES/アットホーム対応。
    新築/未築の場合は CURRENT_YEAR を返す。
    """
    if re.search(r"新築", text):
        return CURRENT_YEAR
    # 「築年月 ... YYYY年M月」 (SUUMO/ノムコム/リバブル詳細)
    m = re.search(r"築年月[^\d]{0,15}(\d{4})年\d{1,2}月", text)
    if m:
        return int(m.group(1))
    # 完成時期（築年月） ... YYYY年M月  (SUUMO別フォーム)
    m = re.search(r"完成時期[^\d]{0,30}(\d{4})年\d{1,2}月", text)
    if m:
        return int(m.group(1))
    # YYYY年M月築 (リバブル card)
    m = re.search(r"(\d{4})年\d{1,2}月築", text)
    if m:
        return int(m.group(1))
    # アットホーム: 築年月 YYYY年M月
    m = re.search(r"(\d{4})年\d{1,2}月\s*（築", text)
    if m:
        return int(m.group(1))
    # ノムコム card: 文中で最後に出る "YYYY年M月"
    matches = re.findall(r"(19[5-9]\d|20[0-2]\d)年\d{1,2}月", text)
    if matches:
        return int(matches[-1])
    return None


def fetch_built_from_detail(url: str, source: str):
    """詳細ページから築年を取得。返せなければNone。"""
    use_cffi = source in ("HOMES", "アットホーム")
    html = fetch(url, impersonate=use_cffi)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    return parse_built(text)


def fill_missing_from_detail(item):
    """欠損フィールドを詳細ページから補完する。怠惰禁止。"""
    needs = []
    if not item.get("price"):
        needs.append("price")
    if not item.get("area"):
        needs.append("area")
    if item["type"] in ("mansion", "house"):
        if not item.get("layout"):
            needs.append("layout")
        if not item.get("built"):
            needs.append("built")
    if not item.get("walk"):
        needs.append("walk")
    if not item.get("addr"):
        needs.append("addr")
    if not needs:
        return

    use_cffi = item["source"] in ("HOMES", "アットホーム")
    html = fetch_with_retry(item["url"], impersonate=use_cffi)
    if not html:
        return
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    for field in needs:
        if field == "price":
            v = parse_price_man(text)
        elif field == "area":
            v = parse_area(text)
        elif field == "layout":
            v = parse_layout(text)
        elif field == "built":
            v = parse_built(text)
        elif field == "walk":
            v = parse_walk(text, item["station"])
        elif field == "addr":
            v = parse_addr(text)
        else:
            continue
        if v:
            item[field] = v


# === SUUMO パーサー（実証済み） ===

def parse_suumo(html: str, station: str, kind: str):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("div.property_unit"):
        a = card.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        if not href.startswith("http"):
            href = "https://suumo.jp" + href
        m = re.search(r"/nc_(\d{6,12})/", href)
        if not m:
            continue
        pid = m.group(1)

        # 物件名は h2 a
        h2 = card.find("h2")
        name = h2.get_text(strip=True) if h2 else ""

        text = card.get_text(" ", strip=True)
        items.append({
            "id": f"suumo:{kind[0]}:{pid}",
            "station": station,
            "type": kind,
            "name": name,
            "price": parse_price_man(text),
            "area": parse_area(text),
            "layout": parse_layout(text),
            "walk": parse_walk(text, station),
            "addr": parse_addr(text),
            "built": parse_built(text),
            "url": href,
            "img": _abs(card_image(card), "https://suumo.jp"),
            "floor": parse_floor(text, kind),
            "source": "SUUMO",
        })
    return items


# === HOMES パーサー ===

def parse_homes(html: str, station: str, kind: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for card in soup.select("div.moduleInner.prg-kksSictClickInfo, div.moduleInner"):
        a = card.find("a", href=lambda h: h and ("/mansion/b-" in h or "/kodate/b-" in h or "/tochi/b-" in h))
        if not a:
            continue
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.homes.co.jp" + href
        m = re.search(r"/(b-\d{10,})", href)
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)

        name_el = card.find(["h3", "h2"])
        name = name_el.get_text(strip=True)[:50] if name_el else a.get_text(strip=True)[:50]

        text = card.get_text(" ", strip=True)
        items.append({
            "id": f"homes:{kind[0]}:{pid}",
            "img": _abs(card_image(card), "https://www.homes.co.jp"),
            "station": station,
            "type": kind,
            "name": name,
            "price": parse_price_man(text),
            "area": parse_area(text),
            "layout": parse_layout(text),
            "walk": parse_walk(text, station),
            "addr": parse_addr(text),
            "built": parse_built(text),
            "url": href,
            "floor": parse_floor(text, kind),
            "source": "HOMES",
        })
    return items


def parse_athome(html: str, station: str, kind: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for card in soup.select("div.card-box-open, div.card-box"):
        a = card.find("a", href=lambda h: h and re.match(r"^/(mansion|kodate|tochi)/\d{10,}", h or ""))
        if not a:
            continue
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.athome.co.jp" + href
        m = re.search(r"/(mansion|kodate|tochi)/(\d{10,12})", href)
        if not m:
            continue
        pid = m.group(2)
        if pid in seen:
            continue
        seen.add(pid)

        name_el = card.find(["h3", "h2"])
        name = name_el.get_text(strip=True)[:50] if name_el else a.get_text(strip=True)[:50]

        text = card.get_text(" ", strip=True)
        items.append({
            "id": f"athome:{kind[0]}:{pid}",
            "img": _abs(card_image(card), "https://www.athome.co.jp"),
            "station": station,
            "type": kind,
            "name": name,
            "price": parse_price_man(text),
            "area": parse_area(text),
            "layout": parse_layout(text),
            "walk": parse_walk(text, station),
            "addr": parse_addr(text),
            "built": parse_built(text),
            "url": href,
            "floor": parse_floor(text, kind),
            "source": "アットホーム",
        })
    return items


# === ノムコム パーサー ===

def parse_athome_rent(html: str, station: str):
    """アットホームの賃貸一覧をパースする。SUUMOと構造が違うので専用。
    1カード=1建物で、中に複数の部屋が入る。
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("[class*=property]"):
        a = card.find("a", href=lambda x: x and re.search(r"/chintai/\d{6,}", x or ""))
        if not a:
            continue
        href = a.get("href") or ""
        m = re.search(r"/chintai/(\d{6,})", href)
        if not m:
            continue
        pid = m.group(1)
        text = card.get_text(" ", strip=True)
        name = text.split(" ")[0][:40]
        # 「ＪＲ横須賀線 「西大井」駅 徒歩9分」形式
        walk = parse_walk(text, station)
        built = parse_built(text)
        addr = parse_addr(text)
        # 「12 万円 15,000円」→ 賃料+管理費
        mr = re.search(r"([\d.]+)\s*万円\s*([\d,]+)円", text)
        if mr:
            rent = float(mr.group(1))
            kanri = int(mr.group(2).replace(",", "")) / 10000.0
            price = round(rent + kanri, 2)
        else:
            mr2 = re.search(r"([\d.]+)\s*万円", text)
            price = float(mr2.group(1)) if mr2 else None
        ma = re.search(r"([\d.]+)\s*m²", text)
        area = float(ma.group(1)) if ma else None
        ml = re.search(r"\b(\d[SLDKR]{1,4})\b", text)
        layout = ml.group(1) if ml else ""
        msr = re.search(r"敷金\s*/\s*礼金", text)
        out.append({
            "id": f"athome:r:{pid}",
            "station": station,
            "type": "rent",
            "name": name,
            "price": price,
            "area": area,
            "layout": layout,
            "walk": walk,
            "addr": addr,
            "built": built,
            "url": _abs(href.split("?")[0], "https://www.athome.co.jp"),
            "img": _abs(card_image(card), "https://www.athome.co.jp"),
            "floor": parse_floor(text, "rent"),
            "source": "アットホーム賃貸",
        })
    return out


# === 三井のリハウス パーサー ===
# 1つのURL(all-type)で中古マンション/新築マンション/戸建/新築戸建/土地が全部返る。
# カード .property-index-card に 価格/住所/路線+駅+徒歩/間取り/面積/築年 が揃う。
_REHOUSE_KIND = {"mansion": "mansion", "s_mansion": "mansion",
                 "kodate": "house", "s_kodate": "house", "tochi": "land"}


def parse_rehouse(html: str, station: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for card in soup.select("div.property-index-card"):
        a = card.find("a", href=lambda h: h and "/bkdetail/" in h)
        if not a:
            continue
        href = a["href"]
        m = re.search(r"/buy/([a-z_]+)/bkdetail/([A-Z0-9]{6,})", href)
        if not m:
            continue
        kind = _REHOUSE_KIND.get(m.group(1))
        if not kind:            # tohshi(投資用)などは対象外
            continue
        pid = m.group(2)
        if pid in seen:
            continue
        seen.add(pid)

        text = card.get_text(" ", strip=True)
        # リハウスの駅ページは近隣駅の物件も混ぜて返す（実測: 目黒駅ページに
        # 桜新町・白金台が入っていた）。カードは最寄り1駅だけを書く。監視駅と違っていても「監視駅から徒歩7分
        # 以内」の可能性があるので捨てず、walk=None にして詳細ページで
        # 監視駅からの分数を判定させる（他条件を満たす物件だけが詳細を見る）。
        sm = re.search(r"[^\s/]*線\s*([^\s]+?)駅\s*徒歩(\d+)分", text)
        walk = int(sm.group(2)) if (sm and sm.group(1) == station) else None
        name_el = card.find(["h2", "h3", "h4"])
        name = (name_el.get_text(strip=True) if name_el else text)[:50]
        items.append({
            "id": f"rehouse:{kind[0]}:{pid}",
            "img": _abs(card_image(card), "https://www.rehouse.co.jp"),
            "station": station,
            "type": kind,
            "name": name,
            "price": parse_price_man(text),
            "area": parse_area(text),
            "layout": parse_layout(text),
            "walk": walk,
            "addr": parse_addr(text),
            "built": parse_built(text),
            "url": _abs(href.split("?")[0], "https://www.rehouse.co.jp"),
            "floor": parse_floor(text, kind),
            "source": "三井のリハウス",
        })
    return items


# スマイティ。建物(section.p-estate)の中に部屋(div.p-estate_grid)が並ぶ二段構造。
# 一覧だけで 価格/間取り/面積/所在階/採光面/築年月/住所/複数駅の徒歩 が全部揃う
# ので詳細ページを見なくて済む（実測: 目黒の中古マンションで建物30件・部屋53件）。
_SUMAITY_TYPE = {"中古マンション": "mansion", "中古一戸建て": "house",
                 "中古住宅": "house", "中古一戸建": "house"}


def _parse_sumaity_flat(soup, station: str, kind: str):
    """スマイティの戸建・土地ページ。建物単位でまとまらず
    div.p-estate__data-wrap が1物件ぶんの価格・間取り・築年月・面積を持つ。
    住所と徒歩は同じカードの上位要素に載る。"""
    items = []
    seen = set()
    for w in soup.select("div.p-estate__data-wrap"):
        a = w.find("a", href=lambda h: h and "/prop_" in h)
        if not a:
            # リンクは写真側にあることがあるのでカード全体から探す
            card = w.parent
            a = card.find("a", href=lambda h: h and "/prop_" in h) if card else None
        if not a:
            continue
        m = re.search(r"/prop_(\d+)/", a["href"])
        if not m or m.group(1) in seen:
            continue
        wtext = w.get_text(" ", strip=True)
        # 住所・徒歩を持つ祖先まで遡る
        ctext = wtext
        cur = w
        for _ in range(5):
            cur = cur.parent
            if cur is None:
                break
            t = cur.get_text(" ", strip=True)
            if parse_addr(t):
                ctext = t
                break
        addr = parse_addr(ctext)
        if not addr:
            continue
        built = None
        mb = re.search(r"\((\d{4})年\d{1,2}月\)", wtext)
        if mb:
            built = int(mb.group(1))
        else:
            mc = re.search(r"築\s*(\d{1,3})\s*年", wtext)
            if mc:
                built = CURRENT_YEAR - int(mc.group(1))
        # 戸建は「建物面積」を専有面積として扱う
        ma = re.search(r"建物面積\s*([\d.]+)", wtext)
        area = float(ma.group(1)) if ma else parse_area(wtext)
        if kind == "land":
            ml = re.search(r"土地面積\s*([\d.]+)", wtext)
            if ml:
                area = float(ml.group(1))
        seen.add(m.group(1))
        items.append({
            "id": f"sumaity:{kind[0]}:{m.group(1)}",
            "img": _abs(card_image(w.parent or w), "https://sumaity.com"),
            "station": station,
            "type": kind,
            "name": addr,
            "price": parse_price_man(wtext),
            "area": area,
            "layout": parse_layout(wtext),
            "walk": parse_walk(ctext.replace("まで 徒歩", " 徒歩"), station),
            "walks": parse_all_walks(ctext.replace("まで 徒歩", " 徒歩")),
            "built": built,
            "floor": parse_floor(wtext, kind),
            "addr": addr,
            "url": _abs(a["href"], "https://sumaity.com"),
            "source": "スマイティ",
            "parking": None,
        })
    return items


def parse_sumaity(html: str, station: str, kind: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    # 戸建・土地は section.p-estate は付くが部屋グリッド(div.p-estate_grid)が
    # 無い別構造なので、グリッドの有無で分岐する（実測: --house-used クラス）
    if not soup.select("div.p-estate_grid"):
        return _parse_sumaity_flat(soup, station, kind)
    for est in soup.select("section.p-estate"):
        bld = est.select_one("div.p-building")
        btext = bld.get_text(" ", strip=True) if bld else ""
        # 「まで 徒歩5分」表記を共通パーサーが読める「徒歩5分」に寄せる
        btext_n = btext.replace("まで 徒歩", " 徒歩").replace("まで徒歩", " 徒歩")
        bname = ""
        if bld:
            for t in bld.stripped_strings:
                if t in _SUMAITY_TYPE or t in ("新着あり", "NEW"):
                    continue
                bname = t
                break
        btype = next((v for k, v in _SUMAITY_TYPE.items() if k in btext), kind)
        addr = parse_addr(btext_n)
        built = None
        mb = re.search(r"(\d{4})年\d{1,2}月", btext)
        if mb:
            built = int(mb.group(1))
        walk = parse_walk(btext_n, station)
        walks = parse_all_walks(btext_n)

        for grid in est.select("div.p-estate_grid"):
            a = grid.find("a", href=lambda h: h and "/prop_" in h)
            if not a:
                continue
            m = re.search(r"/prop_(\d+)/", a["href"])
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            gi = {}
            for d in grid.find_all("div", recursive=False):
                for cl in (d.get("class") or []):
                    if cl.startswith("p-estate_grid-item--"):
                        gi[cl[-1]] = d.get_text(" ", strip=True)
            price = parse_price_man(gi.get("2", ""))
            # 面積は「61.88m 2」のように上付きが分かれるので共通パーサーに任せる
            area = parse_area(gi.get("3", ""))
            layout = parse_layout(gi.get("3", ""))
            floor = parse_floor(gi.get("4", ""), btype)
            items.append({
                "id": f"sumaity:{btype[0]}:{m.group(1)}",
                "img": _abs(card_image(grid), "https://sumaity.com"),
                "station": station,
                "type": btype,
                "name": bname or addr or "スマイティ掲載物件",
                "price": price,
                "area": area,
                "layout": layout,
                "walk": walk,
                "walks": walks,
                "built": built,
                "floor": floor,
                "addr": addr,
                "url": _abs(a["href"], "https://sumaity.com"),
                "source": "スマイティ",
                "parking": None,
            })
    return items


# CHINTAI(賃貸)。section.cassette_item.build が建物、その中の
# tbody.js-detailLinkUrl が部屋。部屋の列は
#   間取図 / 階 / 家賃 / 管理費 / 敷金 / 礼金 / 間取り / 専有面積 / お問合せ
# の順で、建物側に 住所・交通(複数駅)・築年・階建 が載る。
# 実測: 目黒駅ページで建物26件・部屋52件、すべて生HTMLから取れる。
def parse_chintai(html: str, station: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for bld in soup.select("section.cassette_item"):
        btext = bld.get_text(" | ", strip=True)
        name = ""
        nm = re.search(r"賃貸[^|]*\|\s*([^|]{1,40}?)\s*\|\s*住所", btext)
        if nm:
            name = nm.group(1).strip()
        ma = re.search(r"住所\s*\|\s*([^|]{4,40})", btext)
        addr = ma.group(1).strip() if ma else ""
        built = None
        mb = re.search(r"築年\s*\|\s*(\d{4})年", btext)
        if mb:
            built = int(mb.group(1))
        # 交通欄は「南北線/白金台駅 徒歩1分」形式。共通パーサーが読めるよう
        # 路線とのスラッシュを空白にする
        transit = btext.replace("/", " ")
        walk = parse_walk(transit, station)
        walks = parse_all_walks(transit)

        for room in bld.select("tbody.js-detailLinkUrl"):
            a = room.find("a", href=lambda h: h and "/detail/bk-" in h)
            if not a:
                continue
            m = re.search(r"/detail/(bk-\w+)/", a["href"])
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            cells = [td.get_text(" ", strip=True)
                     for td in room.find_all("td")]
            rtext = " ".join(cells)
            floor = parse_floor(rtext, "rent")
            mr = re.search(r"([\d.]+)\s*万円", rtext)
            rent = float(mr.group(1)) if mr else None
            mk = re.search(r"万円\s*([\d,]+)\s*円", rtext)
            kanri = int(mk.group(1).replace(",", "")) if mk else 0
            total = round(rent + kanri / 10000, 2) if rent is not None else None
            # 敷金→礼金の順に並ぶ（ヘッダで実測）
            sk = re.findall(r"(なし|[\d.]+\s*ヶ月|[\d,]+\s*円)", rtext)
            shikirei = ""
            if len(sk) >= 2:
                shikirei = f"敷{sk[-2].replace(' ', '')}/礼{sk[-1].replace(' ', '')}"
            items.append({
                "id": f"chintai:r:{m.group(1)}",
                "img": _abs(card_image(room), "https://www.chintai.net"),
                "station": station,
                "type": "rent",
                "name": name or addr or "CHINTAI掲載物件",
                "price": total,
                "rent": rent,
                "kanri": kanri,
                "area": parse_area(rtext),
                "layout": parse_layout(rtext),
                "walk": walk,
                "walks": walks,
                "built": built,
                "floor": floor,
                "shikirei": shikirei,
                "addr": addr,
                "url": _abs(a["href"], "https://www.chintai.net"),
                "source": "CHINTAI",
                "parking": None,
            })
    return items


# ニフティ不動産(横断検索)。SUUMO/HOMES/アットホーム等の在庫がまとまって出る。
# カード div.box.is-padding-lg.is-space-lg が建物、その中の
# div.box.is-mobile-0.is-space-xs が「業者ごとの掲載」で価格/階/間取り/面積を持つ。
# 一覧の徒歩は「ニフティ不動産が独自で判定した利用可能駅」＝推定値なので使わない。
# walk=None にして、他条件を満たすものだけ詳細ページの「交通機関」欄
# (元サイトの公式表記が載る)から徒歩を取る。
def _parse_nifty_rent(soup, station: str):
    """ニフティの賃貸は tbody.click-area が1部屋。建物情報は上位にある。
    列は 階/間取り/面積/賃料/管理費/敷/礼（実測）。"""
    items = []
    seen = set()
    for tb in soup.select("tbody.click-area"):
        a = tb.find("a", href=lambda h: h and "detail_" in h)
        if not a:
            continue
        m = re.search(r"detail_([0-9a-f]{8,})", a["href"])
        if not m or m.group(1) in seen:
            continue
        t = re.sub(r"\s+", " ", tb.get_text(" ", strip=True))
        # 建物情報を持つ祖先を探す
        btext = ""
        cur = tb
        for _ in range(6):
            cur = cur.parent
            if cur is None:
                break
            bt = re.sub(r"\s+", " ", cur.get_text(" ", strip=True))
            if "徒歩" in bt or "歩" in bt:
                if parse_addr(bt):
                    btext = bt
                    break
        addr = parse_addr(btext)
        if not addr:
            continue
        name = ""
        mn = re.search(r"^([^|]{2,30}?)の賃貸物件", btext)
        if mn:
            name = mn.group(1).strip()
        built = None
        mb = re.search(r"築年数\s*(\d{1,3})\s*年", btext)
        if mb:
            built = CURRENT_YEAR - int(mb.group(1))
        mr = re.search(r"([\d.]+)\s*万円", t)
        rent = float(mr.group(1)) if mr else None
        mk = re.search(r"万円\s*([\d,]+)\s*円", t)
        kanri = int(mk.group(1).replace(",", "")) if mk else 0
        total = round(rent + kanri / 10000, 2) if rent is not None else None
        ms = re.search(r"敷\s*(\S+)\s*礼\s*(\S+)", t)
        shikirei = ""
        if ms:
            def _f(x):
                return "なし" if x in ("不要", "-", "－", "なし") else x
            shikirei = f"敷{_f(ms.group(1))}/礼{_f(ms.group(2))}"
        seen.add(m.group(1))
        items.append({
            "id": f"nifty:r:{m.group(1)}",
            "img": _abs(card_image(tb), "https://myhome.nifty.com"),
            "station": station,
            "type": "rent",
            "name": name or addr,
            "price": total,
            "rent": rent,
            "kanri": kanri,
            "area": parse_area(t),
            "layout": parse_layout(t),
            "walk": None,      # 一覧の徒歩は独自推定なので使わない
            "built": built,
            "floor": parse_floor(t, "rent"),
            "shikirei": shikirei,
            "addr": addr,
            "url": _abs(a["href"], "https://myhome.nifty.com"),
            "source": "ニフティ不動産",
            "parking": None,
        })
    return items


def parse_nifty(html: str, station: str, kind: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    if kind == "rent" and soup.select("tbody.click-area"):
        return _parse_nifty_rent(soup, station)
    items = []
    seen = set()
    # 中古は div.box.is-padding-lg.is-space-lg が1建物。新築はページ構造が
    # 違い div.column.is-mobile-0 が1物件（実測）。中古ページにも前者が
    # 数個だけ存在して物件を含まないことがあるので両方を見て、
    # 物件IDの重複は seen で弾く。
    cards = (soup.select("div.box.is-padding-lg.is-space-lg")
             + soup.select("div.column.is-mobile-0"))
    for card in cards:
        links = card.select('a[href*="detail_"]')
        if not links:
            continue
        ctext = re.sub(r"\s+", " ", card.get_text(" | ", strip=True))
        addr = parse_addr(ctext)
        if not addr:
            continue          # 住所が読めないものは区の判定ができないので採らない
        # 築年月は「22年10ヶ月」という築年数表記（西暦ではない）
        built = None
        mb = re.search(r"築年月\s*\|\s*(\d{1,3})年", ctext)
        if mb:
            built = CURRENT_YEAR - int(mb.group(1))
        else:
            my = re.search(r"築年月\s*\|\s*(\d{4})年", ctext)
            if my:
                built = int(my.group(1))
            elif "新築" in ctext:
                built = CURRENT_YEAR      # 新築は築0年として扱う

        blks = card.select("div.box.is-mobile-0.is-space-xs") or [card]
        for blk in blks:
            a = blk.find("a", href=lambda h: h and "detail_" in h)
            if not a:
                continue
            m = re.search(r"detail_([0-9a-f]{8,})", a["href"])
            if not m or m.group(1) in seen:
                continue
            btext = re.sub(r"\s+", " ", blk.get_text(" ", strip=True))
            price = parse_price_man(btext)
            area = parse_area(btext)
            layout = parse_layout(btext)
            # 「7階 / 2LDK / 67.46m²」。不明は「-階」と出る
            mf = re.search(r"(\d+)\s*階\s*/", btext)
            floor = f"{mf.group(1)}階" if mf else ("—" if kind == "land" else "")
            if price is None and area is None:
                continue      # 価格も面積も無いブロックは掲載枠ではない
            seen.add(m.group(1))
            items.append({
                "id": f"nifty:{kind[0]}:{m.group(1)}",
                "img": _abs(card_image(blk), "https://myhome.nifty.com"),
                "station": station,
                "type": kind,
                "name": addr,          # ニフティの一覧に建物名は出ない
                "price": price,
                "area": area,
                "layout": layout,
                "walk": None,          # 一覧の徒歩は独自推定なので使わない
                "built": built,
                "floor": floor,
                "addr": addr,
                "url": _abs(a["href"], "https://myhome.nifty.com"),
                "source": "ニフティ不動産",
                "parking": None,
            })
    return items


# ハウスコム(賃貸)。建物 article の中に article.property_room が部屋として並ぶ。
# 建物側に 物件名/所在地/最寄駅(複数・徒歩)/築年月/構造/階数、
# 部屋側に 号室/賃料/共益費/敷礼保償/階/間取り/面積 が載る（実測）。
def parse_housecom(html: str, station: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for room in soup.select("article.property_room"):
        a = room.find("a", href=lambda h: h and re.match(r"/room_\d+/", h or ""))
        if not a:
            continue
        m = re.match(r"/room_(\d+)/", a["href"])
        if not m or m.group(1) in seen:
            continue
        bld = room.find_parent("article")
        btext = bld.get_text(" ", strip=True) if bld else ""
        rtext = room.get_text(" ", strip=True)
        name = ""
        if bld:
            for tg in bld.stripped_strings:
                if tg not in ("賃貸マンション", "賃貸アパート", "賃貸一戸建て"):
                    name = tg
                    break
        # 「所在地 東京都 目黒区 三田 最寄駅 …」と続くので最寄駅より前で切る。
        # 切らずに空白を詰めると住所に「最寄駅山手線目黒駅」まで入ってしまう。
        aseg = btext.split("最寄駅")[0]
        m2 = re.search(r"所在地\s*(.+)$", aseg)
        addr = parse_addr((m2.group(1) if m2 else aseg).replace(" ", ""))
        if not addr:
            continue
        built = None
        mb = re.search(r"築年月\s*(\d{4})年", btext)
        if mb:
            built = int(mb.group(1))
        # 「（徒歩10分）」形式なので共通パーサーが読める形に寄せる
        transit = btext.replace("（徒歩", " 徒歩").replace("）", " ")
        walk = parse_walk(transit, station)
        walks = parse_all_walks(transit)
        mr = re.search(r"([\d.]+)\s*万円", rtext)
        rent = float(mr.group(1)) if mr else None
        mk = re.search(r"共益費\s*([\d,]+)\s*円", rtext)
        kanri = int(mk.group(1).replace(",", "")) if mk else 0
        total = round(rent + kanri / 10000, 2) if rent is not None else None
        ms = re.search(r"敷\s*(\S+)\s*礼\s*(\S+)", rtext)
        shikirei = ""
        if ms:
            def _f(x):
                return "なし" if x in ("－", "-", "―", "0", "0円") else x
            shikirei = f"敷{_f(ms.group(1))}/礼{_f(ms.group(2))}"
        seen.add(m.group(1))
        items.append({
            "id": f"housecom:r:{m.group(1)}",
            "img": _abs(card_image(room), "https://www.housecom.jp"),
            "station": station,
            "type": "rent",
            "name": name or addr,
            "price": total,
            "rent": rent,
            "kanri": kanri,
            "area": parse_area(rtext),
            "layout": parse_layout(rtext),
            "walk": walk,
            "walks": walks,
            "built": built,
            "floor": parse_floor(rtext, "rent"),
            "shikirei": shikirei,
            "addr": addr,
            "url": _abs(a["href"], "https://www.housecom.jp"),
            "source": "ハウスコム",
            "parking": None,
        })
    return items


# 東急リバブルの賃貸。div[class*=Card_propertyCardContents] が1部屋で、
# 名前/賃料/管理費/住所/駅徒歩/敷礼/間取り/面積/築年月/所在階 が全部載る。
# 賃料は「40 万 5,000 円」のように万と円が分かれて出る（実測）。
def parse_livable_rent(html: str, station: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for card in soup.select("div[class*=Card_propertyCardContents]"):
        a = card.find("a", href=lambda h: h and re.match(r"^/chintai/L\d+/?$", h or ""))
        if not a:
            continue
        m = re.search(r"/chintai/(L\d+)/?", a["href"])
        if not m or m.group(1) in seen:
            continue
        t = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        # 「40 万 5,000 円」→ 40.5万。「28 万 円」→ 28万
        rent = None
        mr = re.search(r"([\d,]+)\s*万\s*(?:([\d,]+)\s*円)?", t)
        if mr:
            man = float(mr.group(1).replace(",", ""))
            yen = float(mr.group(2).replace(",", "")) if mr.group(2) else 0.0
            rent = round(man + yen / 10000, 2)
        mk = re.search(r"管理費\s*([\d,]+)\s*円", t)
        kanri = int(mk.group(1).replace(",", "")) if mk else 0
        total = round(rent + kanri / 10000, 2) if rent is not None else None
        addr = parse_addr(t)
        if not addr:
            continue
        built = None
        mb = re.search(r"(\d{4})年\d{1,2}月築", t)
        if mb:
            built = int(mb.group(1))
        # 「25階建ての16階部分」→ 16階
        floor = ""
        mf = re.search(r"階建ての\s*(B?\d+)\s*階", t)
        if mf:
            floor = f"{mf.group(1)}階"
        else:
            floor = parse_floor(t, "rent")
        ms = re.search(r"敷金\s*(\S+?)\s*礼金\s*(\S+)", t)
        shikirei = ""
        if ms:
            def _f(x):
                return "なし" if x in ("-", "－", "―", "0", "0円", "なし") else x
            shikirei = f"敷{_f(ms.group(1))}/礼{_f(ms.group(2))}"
        # 物件名は h2。テキストから正規表現で切り出すとUI文言("間取り"
        # "NEW 9/4" など)を拾ってしまうので必ず h2 から取る（実測）
        h2 = card.find("h2")
        name = h2.get_text(strip=True) if h2 else ""
        seen.add(m.group(1))
        items.append({
            "id": f"livable:r:{m.group(1)}",
            "img": _abs(card_image(card), "https://www.livable.co.jp"),
            "station": station,
            "type": "rent",
            "name": name or addr,
            "price": total,
            "rent": rent,
            "kanri": kanri,
            "area": parse_area(t),
            "layout": parse_layout(t),
            "walk": parse_walk(t, station),
            "walks": parse_all_walks(t),
            "built": built,
            "floor": floor,
            "shikirei": shikirei,
            "addr": addr,
            "url": _abs(a["href"], "https://www.livable.co.jp"),
            "source": "リバブル賃貸",
            "parking": None,
        })
    return items


# スマイティの賃貸。div.building が建物、tr.estate.applicable が部屋。
# 列は 画像 / 階 / 賃料 / 管理費 / 敷 / 礼 / 間取り / 面積 / 向き（実測）。
# 建物側に 種別/名前/複数駅の徒歩/住所/築年数/構造/総階数 が載る。
def parse_sumaity_rent(html: str, station: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for bld in soup.select("div.building"):
        btext = re.sub(r"\s+", " ", bld.get_text(" | ", strip=True))
        name = ""
        mn = re.search(r"(?:マンション|アパート|一戸建て|テラスハウス)\s*\|\s*([^|]{1,40}?)\s*\|", btext)
        if mn:
            name = mn.group(1).strip()
        addr = parse_addr(btext)
        if not addr:
            continue
        built = None
        mb = re.search(r"築年数\s*\|\s*築\s*(\d{1,3})\s*年", btext)
        if mb:
            built = CURRENT_YEAR - int(mb.group(1))
        elif "新築" in btext:
            built = CURRENT_YEAR
        transit = btext.replace("|", " ")
        walk = parse_walk(transit, station)
        walks = parse_all_walks(transit)
        for tr in bld.select("tr.estate"):
            a = tr.find("a", href=lambda h: h and "/prop_" in h)
            if not a:
                continue
            m = re.search(r"/prop_(\d+)/", a["href"])
            if not m or m.group(1) in seen:
                continue
            t = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
            mr = re.search(r"([\d.]+)\s*万円", t)
            rent = float(mr.group(1)) if mr else None
            mk = re.search(r"万円\s*([\d,]+)\s*円", t)
            kanri = int(mk.group(1).replace(",", "")) if mk else 0
            total = round(rent + kanri / 10000, 2) if rent is not None else None
            ms = re.search(r"敷\s*(\S+?)\s*礼\s*(\S+?)\s", t + " ")
            shikirei = ""
            if ms:
                def _f(x):
                    return "なし" if x in ("-", "－", "―", "0円", "なし") else x
                shikirei = f"敷{_f(ms.group(1))}/礼{_f(ms.group(2))}"
            seen.add(m.group(1))
            items.append({
                "id": f"sumaity:r:{m.group(1)}",
                "img": _abs(card_image(tr), "https://sumaity.com"),
                "station": station,
                "type": "rent",
                "name": name or addr,
                "price": total,
                "rent": rent,
                "kanri": kanri,
                "area": parse_area(t),
                "layout": parse_layout(t),
                "walk": walk,
                "walks": walks,
                "built": built,
                "floor": parse_floor(t, "rent"),
                "shikirei": shikirei,
                "addr": addr,
                "url": _abs(a["href"], "https://sumaity.com"),
                "source": "スマイティ賃貸",
                "parking": None,
            })
    return items


# 賃貸スモッカ。div.item_list01.bukken が建物、table.item_list01_rooms の
# 各行が部屋で、列は 階/部屋番号/賃料/管理費/敷金/礼金/間取/面積/方位（実測）。
# 駅コードはノムコムと同じ国交省体系がそのまま使える（22駅で実測確認）。
def parse_smocca(html: str, station: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for bld in soup.select("div.item_list01.bukken"):
        btext = re.sub(r"\s+", " ", bld.get_text(" | ", strip=True))
        addr = parse_addr(btext)
        if not addr:
            continue
        name = ""
        h_el = bld.find(["h2", "h3", "p"])
        if h_el:
            name = h_el.get_text(strip=True)[:40]
        built = None
        mb = re.search(r"(\d{4})年\d{1,2}月", btext)
        if mb:
            built = int(mb.group(1))
        elif "新築" in btext:
            built = CURRENT_YEAR
        transit = btext.replace("/", " ").replace("|", " ").replace("歩", "徒歩")
        transit = transit.replace("徒徒歩", "徒歩")
        walk = parse_walk(transit, station)
        walks = parse_all_walks(transit)
        for tr in bld.select("table.item_list01_rooms tr"):
            a = tr.find("a", href=lambda h: h and "/bukken/detail/" in h)
            if not a:
                continue          # ヘッダ行
            m = re.search(r"/bukken/detail/(\w+)", a["href"])
            if not m or m.group(1) in seen:
                continue
            t = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
            mr = re.search(r"([\d.]+)\s*万円", t)
            rent = float(mr.group(1)) if mr else None
            mk = re.search(r"万円\s*([\d,]+)\s*円", t)
            kanri = int(mk.group(1).replace(",", "")) if mk else 0
            total = round(rent + kanri / 10000, 2) if rent is not None else None
            # 敷金・礼金は賃料のあとに2つ並ぶ
            sk = re.findall(r"([\d.]+万円|なし|－|-)", t)
            shikirei = ""
            if len(sk) >= 3:
                def _f(x):
                    return "なし" if x in ("-", "－", "なし") else x
                shikirei = f"敷{_f(sk[1])}/礼{_f(sk[2])}"
            seen.add(m.group(1))
            items.append({
                "id": f"smocca:r:{m.group(1)}",
                "img": _abs(card_image(tr) or card_image(bld), "https://smocca.jp"),
                "station": station,
                "type": "rent",
                "name": name or addr,
                "price": total,
                "rent": rent,
                "kanri": kanri,
                "area": parse_area(t),
                "layout": parse_layout(t),
                "walk": walk,
                "walks": walks,
                "built": built,
                "floor": parse_floor(t, "rent"),
                "shikirei": shikirei,
                "addr": addr,
                "url": a["href"] if a["href"].startswith("http") else _abs(a["href"], "https://smocca.jp"),
                "source": "賃貸スモッカ",
                "parking": None,
            })
    return items


# goo住宅・不動産の賃貸。div.name_id が建物、その中の tr(td.property-img を
# 持つ行)が部屋。列は 階/賃料/管理費/敷礼保証/間取り/広さ（実測）。
# 駅コードはノムコムと同じ体系で、路線は先頭1桁を落とす（2172→172）。
def parse_goo(html: str, station: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for bld in soup.select("div.name_id"):
        btext = re.sub(r"\s+", " ", bld.get_text(" | ", strip=True))
        addr = parse_addr(btext)
        if not addr:
            continue
        name = ""
        first = next(bld.stripped_strings, "")
        if first and "|" not in first:
            name = first[:40]
        built = None
        mb = re.search(r"築\s*(\d{1,3})\s*年", btext)
        if mb:
            built = CURRENT_YEAR - int(mb.group(1))
        elif "新築" in btext:
            built = CURRENT_YEAR
        transit = btext.replace("|", " ")
        walk = parse_walk(transit, station)
        walks = parse_all_walks(transit)
        for td in bld.select("td.property-img"):
            tr = td.find_parent("tr")
            if tr is None:
                continue
            a = tr.find("a", href=lambda h: h and "/rent/ap/detail/" in h)
            if not a:
                continue
            m = re.search(r"/x(\w+)\.html", a["href"])
            key = m.group(1) if m else a["href"]
            if key in seen:
                continue
            t = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
            mr = re.search(r"([\d.]+)\s*万円", t)
            rent = float(mr.group(1)) if mr else None
            mk = re.search(r"万円\s*([\d,]+)\s*円", t)
            kanri = int(mk.group(1).replace(",", "")) if mk else 0
            total = round(rent + kanri / 10000, 2) if rent is not None else None
            ms = re.search(r"円\s*([^\s/]+)\s*/\s*([^\s/]+)", t)
            shikirei = ""
            if ms:
                def _f(x):
                    return "なし" if x in ("-", "－", "なし") else x
                shikirei = f"敷{_f(ms.group(1))}/礼{_f(ms.group(2))}"
            seen.add(key)
            items.append({
                "id": f"goo:r:{key}",
                "img": _abs(card_image(tr), "https://house.goo.ne.jp"),
                "station": station,
                "type": "rent",
                "name": name or addr,
                "price": total,
                "rent": rent,
                "kanri": kanri,
                "area": parse_area(t),
                "layout": parse_layout(t),
                "walk": walk,
                "walks": walks,
                "built": built,
                "floor": parse_floor(t, "rent"),
                "shikirei": shikirei,
                "addr": addr,
                "url": _abs(a["href"], "https://house.goo.ne.jp"),
                "source": "goo住宅",
                "parking": None,
            })
    return items


# goo住宅・不動産の売買。div.nayose-property-data が1物件で
# 価格/間取り/広さ/階数/方位 を持ち、住所と徒歩は上位要素にある（実測）。
def parse_goo_buy(html: str, station: str, kind: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    boxes = soup.select("div.nayose-property-data")
    if not boxes:
        # 戸建・土地・新築は table.tab.property 全体で1物件（実測）。
        # 価格/間取り/建物面積/土地面積/築年月/所在地/複数駅の徒歩 が入る
        boxes = [t for t in soup.select("table.tab.property")
                 if t.find("a", href=lambda h: h and "/detail/" in (h or ""))]
    for box in boxes:
        a = box.find("a", href=lambda h: h and "/detail/" in h)
        if not a:
            par = box.parent
            for _ in range(4):
                if par is None:
                    break
                a = par.find("a", href=lambda h: h and "/buy/" in (h or "") and "/detail/" in h)
                if a:
                    break
                par = par.parent
        if not a:
            continue
        m = re.search(r"/x(\w+)\.html", a["href"])
        key = m.group(1) if m else a["href"]
        if key in seen:
            continue
        t = re.sub(r"\s+", " ", box.get_text(" ", strip=True))
        # 住所・徒歩を持つ祖先を探す
        ctext = t
        cur = box
        for _ in range(6):
            cur = cur.parent
            if cur is None:
                break
            bt = re.sub(r"\s+", " ", cur.get_text(" ", strip=True))
            if parse_addr(bt):
                ctext = bt
                break
        addr = parse_addr(ctext)
        if not addr:
            continue
        built = None
        mb = re.search(r"(\d{4})年\d{1,2}月", ctext)
        if mb:
            built = int(mb.group(1))
        else:
            mc = re.search(r"築\s*(\d{1,3})\s*年", ctext)
            if mc:
                built = CURRENT_YEAR - int(mc.group(1))
            elif "新築" in ctext:
                built = CURRENT_YEAR
        mf = re.search(r"階数\s*(B?\d+)\s*階", t)
        floor = f"{mf.group(1)}階" if mf else parse_floor(t, kind)
        # 「90.09m 2 | 56.61m 2」は 建物面積→土地面積 の順（実測）
        area_over = None
        ms2 = re.findall(r"([\d.]+)\s*m\s*2", t)
        if ms2:
            if kind == "land" and len(ms2) >= 2:
                area_over = float(ms2[1])
            else:
                area_over = float(ms2[0])
        name = ""
        h_el = None
        cur2 = box
        for _ in range(4):
            cur2 = cur2.parent
            if cur2 is None:
                break
            h_el = cur2.find(["h2", "h3"])
            if h_el:
                break
        if h_el:
            name = h_el.get_text(strip=True)[:40]
        seen.add(key)
        items.append({
            "id": f"goo:{kind[0]}:{key}",
            "img": _abs(card_image(box), "https://house.goo.ne.jp"),
            "station": station,
            "type": kind,
            "name": name or addr,
            "price": parse_price_man(t),
            "area": area_over if area_over is not None else parse_area(t),
            "layout": parse_layout(t),
            "walk": parse_walk(ctext, station),
            "walks": parse_all_walks(ctext),
            "built": built,
            "floor": floor,
            "addr": addr,
            "url": _abs(a["href"], "https://house.goo.ne.jp"),
            "source": "goo住宅",
            "parking": None,
        })
    return items


# カウカモ（中古マンション・リノベ物件）。div.p-entry が1物件で
# 面積/間取り/価格/駅徒歩/住所 が載る。築年は一覧に出ないので
# 詳細ページから補完させる（walk はカードにあるのでそのまま使う）。
def parse_cowcamo(html: str, station: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for e in soup.select("div.p-entry"):
        a = e.find("a", href=True)
        if not a:
            continue
        m = re.search(r"/([A-Za-z0-9_-]{6,})$", a["href"])
        if not m or m.group(1) in seen:
            continue
        t = re.sub(r"\s+", " ", e.get_text(" ", strip=True))
        addr = parse_addr(t)
        if not addr:
            continue
        mw = re.search(rf"{re.escape(station)}駅\s*徒歩\s*(\d+)\s*分", t)
        walk = int(mw.group(1)) if mw else None
        seen.add(m.group(1))
        items.append({
            "id": f"cowcamo:m:{m.group(1)}",
            "img": _abs(card_image(e), "https://cowcamo.jp"),
            "station": station,
            "type": "mansion",
            "name": addr,          # 一覧に物件名は出ない
            "price": parse_price_man(t),
            "area": parse_area(t),
            "layout": parse_layout(t),
            "walk": walk,
            "built": None,         # 詳細ページから補完する
            "floor": "",
            "addr": addr,
            "url": _abs(a["href"], "https://cowcamo.jp"),
            "source": "カウカモ",
            "parking": None,
        })
    return items


# HOMESの賃貸。div.unitList が建物、tbody.prg-roomList の各行が部屋。
# 列は 階/部屋番号/賃料/管理費/敷金/礼金/保証/敷引/間取り/専有面積（実測）。
def parse_homes_rent(html: str, station: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    for bld in soup.select("div.unitList"):
        btext = re.sub(r"\s+", " ", bld.get_text(" | ", strip=True))
        # 建物情報は上位にあることがあるので親も見る
        ctext = btext
        cur = bld
        for _ in range(4):
            cur = cur.parent
            if cur is None:
                break
            ct = re.sub(r"\s+", " ", cur.get_text(" | ", strip=True))
            if parse_addr(ct):
                ctext = ct
                break
        addr = parse_addr(ctext)
        if not addr:
            continue
        name = ""
        h_el = bld.find(["h2", "h3"]) or (cur.find(["h2", "h3"]) if cur else None)
        if h_el:
            name = h_el.get_text(strip=True)[:40]
        if name.startswith("掲載物件"):
            name = ""          # 一覧のUI文言。物件名ではない
        built = None
        # 「築年数/階数 | 12年 / 7階建」の形（実測）
        mb = re.search(r"築年数[^|]*\|\s*(\d{1,3})\s*年", ctext)
        if not mb:
            mb = re.search(r"築\s*(\d{1,3})\s*年", ctext)
        if mb:
            built = CURRENT_YEAR - int(mb.group(1))
        else:
            my = re.search(r"(\d{4})年\s*\d{0,2}月?\s*築", ctext)
            if my:
                built = int(my.group(1))
        # 「新築」表記は築0年
        if built is None and "新築" in ctext:
            built = CURRENT_YEAR
        walk = parse_walk(ctext.replace("|", " "), station)
        walks = parse_all_walks(ctext.replace("|", " "))
        for tr in bld.select("tbody.prg-roomList tr"):
            a = tr.find("a", href=lambda h: h and "/chintai/room/" in (h or ""))
            if not a:
                continue
            m = re.search(r"/chintai/room/(\w+)/", a["href"])
            if not m or m.group(1) in seen:
                continue
            t = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
            mr = re.search(r"([\d.]+)\s*万円", t)
            rent = float(mr.group(1)) if mr else None
            mk = re.search(r"万円\s*/\s*([\d,]+)\s*円", t)
            kanri = int(mk.group(1).replace(",", "")) if mk else 0
            total = round(rent + kanri / 10000, 2) if rent is not None else None
            ms = re.search(r"([^\s/]+)\s*/\s*([^\s/]+)\s*/\s*[^\s/]+\s*/\s*[^\s/]+", t)
            shikirei = ""
            if ms:
                def _f(x):
                    return "なし" if x in ("-", "－", "無", "なし") else x
                shikirei = f"敷{_f(ms.group(1))}/礼{_f(ms.group(2))}"
            seen.add(m.group(1))
            items.append({
                "id": f"homes:r:{m.group(1)}",
                "img": _abs(card_image(tr), "https://www.homes.co.jp"),
                "station": station,
                "type": "rent",
                "name": name or addr,
                "price": total,
                "rent": rent,
                "kanri": kanri,
                "area": parse_area(t),
                "layout": parse_layout(t),
                "walk": walk,
                "walks": walks,
                "built": built,
                "floor": parse_floor(t, "rent"),
                "shikirei": shikirei,
                "addr": addr,
                "url": a["href"] if a["href"].startswith("http") else _abs(a["href"], "https://www.homes.co.jp"),
                "source": "HOMES賃貸",
                "parking": None,
            })
    return items


def parse_nomu(html: str, station: str, kind: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    # ノムコム検索結果カード
    for card in soup.select("div.item_resultsmall, div.click_R"):
        a = card.find("a", href=lambda h: h and "/id/" in h)
        if not a:
            continue
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.nomu.com" + href
        m = re.search(r"/id/([A-Z0-9]{8,})", href)
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)

        name_el = card.find(["h3", "h2"])
        name = name_el.get_text(strip=True)[:50] if name_el else a.get_text(strip=True)[:50]

        text = card.get_text(" ", strip=True)
        items.append({
            "id": f"nomu:{kind[0]}:{pid}",
            "img": _abs(card_image(card), "https://www.nomu.com"),
            "station": station,
            "type": kind,
            "name": name,
            "price": parse_price_man(text),
            "area": parse_area(text),
            "layout": parse_layout(text),
            "walk": parse_walk(text, station),
            "addr": parse_addr(text),
            "built": parse_built(text),
            "url": href,
            "floor": parse_floor(text, kind),
            "source": "ノムコム",
        })
    return items


# === 東急リバブル パーサー ===

def parse_livable(html: str, station: str, kind: str):
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()
    # リバブルはCSS-modulesでクラス名がハッシュ化される。aタグから辿る
    for a in soup.find_all("a", href=lambda h: h and re.search(r"/(?:mansion|kodate|tochi)/C\d{6,12}", h or "")):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.livable.co.jp" + href
        m = re.search(r"/(C\d{6,12})", href)
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)

        # aの祖先からカード相当のコンテナを探す
        card = a
        for _ in range(6):
            if not card.parent:
                break
            card = card.parent
            text = card.get_text(" ", strip=True)
            if "万円" in text and "m" in text:
                break

        text = card.get_text(" ", strip=True)
        name_el = card.find(["h2", "h3"])
        name = name_el.get_text(strip=True)[:50] if name_el else a.get_text(strip=True)[:50]

        items.append({
            "id": f"livable:{kind[0]}:{pid}",
            "img": _abs(card_image(a), "https://www.livable.co.jp"),
            "station": station,
            "type": kind,
            "name": name,
            "price": parse_price_man(text),
            "area": parse_area(text),
            "layout": parse_layout(text),
            "walk": parse_walk(text, station),
            "addr": parse_addr(text),
            "built": parse_built(text),
            "url": href,
            "floor": parse_floor(text, kind),
            "source": "リバブル",
        })
    return items


# === SUUMO 賃貸パーサー ===

def parse_suumo_rent(html: str, station: str):
    """SUUMO賃貸ページをパース。1建物に複数部屋があるので部屋単位で返す。
    賃料は「賃料＋管理費」の合計(万円)で price_total に入れる。
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for c in soup.select("div.cassetteitem"):
        # --- 建物共通情報 ---
        t = c.select_one(".cassetteitem_content-title")
        bname = t.get_text(strip=True) if t else "賃貸物件"
        # "ＪＲ山手線 五反田駅 9階建 築22年" のような無名物件は住所ベースの名前にする
        if re.search(r"(線|駅).*(階建|築\d+年)", bname) or len(bname) < 3:
            bname = None  # 後で住所から生成
        a1 = c.select_one(".cassetteitem_detail-col1")
        addr = a1.get_text(strip=True) if a1 else ""
        col2 = c.select_one(".cassetteitem_detail-col2")
        station_text = col2.get_text(" ", strip=True) if col2 else ""
        col3 = c.select_one(".cassetteitem_detail-col3")
        col3_text = col3.get_text(" ", strip=True) if col3 else ""

        # 対象駅の徒歩分（「大井町駅 歩5分」形式）
        walk = None
        m = re.search(rf"{re.escape(station)}駅\s*歩\s*(\d+)\s*分", station_text)
        if m:
            walk = int(m.group(1))
        else:
            m = re.search(r"歩\s*(\d+)\s*分", station_text)
            if m:
                walk = int(m.group(1))

        # 築年
        built = None
        mb = re.search(r"築\s*(\d+)\s*年", col3_text)
        if mb:
            built = CURRENT_YEAR - int(mb.group(1))
        elif "新築" in col3_text:
            built = CURRENT_YEAR

        # --- 部屋単位 ---
        for tr in c.select("table.cassetteitem_other tbody tr"):
            link = tr.find("a", href=lambda h: h and "jnc_" in h)
            if not link:
                continue
            href = link["href"]
            if not href.startswith("http"):
                href = "https://suumo.jp" + href
            mid = re.search(r"(jnc_\d+)", href)
            if not mid:
                continue
            pid = mid.group(1)

            cells = [td.get_text(" ", strip=True) for td in tr.select("td")]
            row_text = " | ".join(cells)

            # 賃料(万円) と 管理費(円 or 万円)
            rent = None
            mrent = re.search(r"([\d.]+)\s*万円", row_text)
            if mrent:
                try:
                    rent = float(mrent.group(1))
                except ValueError:
                    rent = None
            # 管理費: "8000円" or "-" or "0.8万円"
            kanri = 0.0
            mk = re.search(r"(\d[\d,]*)\s*円(?!\s*/)", row_text)
            if mk:
                try:
                    kanri = int(mk.group(1).replace(",", "")) / 10000.0
                except ValueError:
                    kanri = 0.0
            if rent is None:
                continue
            total = round(rent + kanri, 2)

            layout = parse_layout(row_text)
            area = parse_area(row_text)
            # 敷金/礼金（td[4]相当）。"23.6万円 -" のように敷金→礼金の順。
            shikirei = ""
            raw_sr = cells[4] if len(cells) > 4 else ""
            if raw_sr:
                parts = raw_sr.split()
                if len(parts) >= 2:
                    def _fmt(x):
                        x = x.strip()
                        return "なし" if x in ("-", "‐", "―", "0円", "0万円") else x
                    shikirei = f"敷{_fmt(parts[0])}/礼{_fmt(parts[1])}"
                elif len(parts) == 1:
                    shikirei = f"敷礼{parts[0]}"

            floor = ""
            mf = re.search(r"(B?\d+階)", row_text)
            if mf:
                floor = mf.group(1)

            disp_name = bname or (addr.replace("東京都", "") + " 賃貸" if addr else "賃貸物件")
            items.append({
                "id": f"suumo:r:{pid}",
                "img": _abs(card_image(c), "https://suumo.jp"),
                "station": station,
                "type": "rent",
                "name": disp_name,
                "price": total,          # 管理費込み(万円)
                "rent": rent,
                "kanri": kanri,
                "area": area,
                "layout": layout,
                "walk": walk,
                "built": built,
                "floor": floor,
                "shikirei": shikirei,
                "addr": addr,
                "url": href,
                "source": "SUUMO賃貸",
                "parking": None,
            })
    return items


def _row_value(soup, *labels):
    """<th>ラベル</th><td>値</td> / <dt>/<dd> から値を取る"""
    for tag in soup.find_all(["th", "dt"]):
        t = tag.get_text(" ", strip=True)
        if any(t.startswith(l) for l in labels):
            nxt = tag.find_next_sibling()
            if nxt:
                return nxt.get_text(" | ", strip=True)
    return ""


PARK_PRICE_RE = re.compile(
    r"(\d[\d,]*\s*万?\s*\d*\s*円(?:\s*[~〜ー-]\s*\d[\d,]*\s*万?\s*\d*\s*円)?)")


def parking_price(v: str):
    """駐車場欄から金額表記を抜く。'2万円～2万3000円／月' などをそのまま返す"""
    if not v:
        return ""
    t = v.replace(" ", "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    for m in PARK_PRICE_RE.finditer(t):
        price = m.group(1)
        # 「0万円」「0円」など無意味な値は採用しない
        if re.fullmatch(r"0*円|0*万円|0*万0*円", price.replace(",", "")):
            continue
        # 45737円 のような生の数字は 45,737円 に整える
        def _comma(mm):
            return f"{int(mm.group(1)):,}円"
        price = re.sub(r"(\d{4,})円", _comma, price)
        if "月" in v and not price.endswith("/月"):
            price += "/月"
        return price
    return ""


def classify_parking(v: str):
    """駐車場欄の値を 有/近隣/空無/無 に分類。判定不能は None"""
    if not v:
        return None
    v = v.strip()
    if v in ("-", "‐", "―", "−") or v.startswith("-"):
        return None                      # SUUMOは未記載を "-" で出す
    if "近隣" in v or "近く" in v:
        return "近隣"
    if "空無" in v or "空き無" in v or "満車" in v:
        return "空無"
    if re.fullmatch(r"(無|なし|無し|空無)", v):
        return "無"
    if ("敷地内" in v or "空有" in v or "有" in v or "円" in v
            or "駐車場" in v or "台" in v):
        return "有"
    if "無" in v and "有" not in v:
        return "無"
    return None


WALK_RE = re.compile(r"[「/]\s*([^「」/｜|]{1,12}?)駅?」?\s*歩\s*(\d{1,3})\s*分")


def parse_all_walks(text: str):
    """交通欄から (駅名, 分) を全部拾う。近い順、駅名重複は最短を採用。"""
    best = {}
    for m in WALK_RE.finditer(text):
        name = m.group(1).strip().rstrip("駅")
        mins = int(m.group(2))
        if not name:
            continue
        if name not in best or mins < best[name]:
            best[name] = mins
    return sorted(best.items(), key=lambda kv: kv[1])


def enrich_from_detail(item):
    """詳細ページを1回だけ取得し、駐車場と『全駅からの徒歩』を埋める"""
    use_cffi = item.get("source") in ("HOMES", "アットホーム",
                                      "アットホーム賃貸", "三井のリハウス",
                                      "スマイティ", "CHINTAI", "ニフティ不動産",
                                      "ハウスコム", "賃貸スモッカ", "goo住宅",
                                      "カウカモ", "HOMES賃貸")
    html = fetch_with_retry(item["url"], impersonate=use_cffi)
    if not html:
        return
    soup = BeautifulSoup(html, "html.parser")

    if item.get("parking") is None:
        raw = _row_value(soup, "駐車場", "駐車", "駐輪・駐車")
        pk = classify_parking(raw)
        if pk is None:
            # 表に無い/"-" の場合は本文（備考・設備欄）から拾う
            body = soup.get_text(" ", strip=True)
            m = re.search(r"駐車場[:：\s]{0,3}([^。\n]{1,40})", body)
            if m:
                raw2 = m.group(1)
                pk2 = classify_parking(raw2)
                if pk2:
                    pk, raw = pk2, raw2
        item["parking"] = pk
        item["parking_price"] = parking_price(raw)

    # 築年: 一覧に無くても詳細の「完成時期（築年月）」「築年月」に必ず載っている
    if not item.get("built") and item.get("type") in ("mansion", "house", "rent"):
        # 本文全体から拾うと無関係な日付(引渡し時期など)を築年と誤認するので、
        # 「築年月」等のラベル行からのみ取る
        row = _row_value(soup, "完成時期", "築年月", "建築年月", "竣工", "築年数")
        b = parse_built(row) if row else None
        if b:
            item["built"] = b

    # 所在階: SUUMOの売買一覧には階の表記が無いので詳細から必ず埋める
    if not item.get("floor"):
        kind = item.get("type") or "mansion"
        if kind == "land":
            item["floor"] = "—"
        else:
            # 戸建はSUUMOでは「構造・工法」に「木造3階建」と載る（実測）
            row = _row_value(soup, "所在階", "階数", "所在階/構造", "所在階／構造",
                             "構造・階建", "構造・階建て", "建物構造",
                             "構造・工法", "構造", "階建")
            f = parse_floor(row, kind) if row else ""
            if not f:
                body = soup.get_text(" ", strip=True)
                m = re.search(r"所在階[:：\s|]{0,3}([^。\n|]{1,16})", body)
                if m:
                    f = parse_floor(m.group(1), kind)
            if f:
                item["floor"] = f

    if not item.get("walks"):
        transit = _row_value(soup, "交通", "駅徒歩", "最寄") or soup.get_text(" ", strip=True)
        if item.get("source") == "ニフティ不動産":
            # 「利用可能駅（ニフティ不動産調べ）」以降は独自推定なので切り捨て、
            # その前にある元サイトの公式表記だけを使う（実測で確認）
            transit = re.split(r"（ニフティ不動産調べ）|\(ニフティ不動産調べ\)",
                               transit)[0]
        walks = parse_all_walks(transit)
        if walks:
            item["walks"] = walks


# === コレクター ===

# HOMES/アットホームがbot検知でブロックし始めたら、そのホストへのアクセスを
# 一定時間止める。ブロック中に叩き続けるとブロックが延びるだけで無駄。
_BLOCKED_UNTIL = {}
_BLOCK_LOCK = threading.Lock()
BLOCK_COOLDOWN = 300  # 秒

# 取得全体の締切。これを超えたら残りの取得を諦めて集計・公開へ進む。
# 実測: アットホームがActionsのIPを完全にブロックしており、180秒休憩付きの
# 再試行を251回繰り返して3時間を使い切り、ページ生成まで到達せずに
# タイムアウトで落ちた（2026-09-09 run 34375924565）。
# 「1サイトが死んでも必ず公開まで届く」ことを最優先にする。
# 取得元が18系統に増え、取得だけで140分に届くようになった。
# 詳細取得とページ生成に十分な時間を残すため110分に縮める。
FETCH_BUDGET_SEC = int(os.environ.get("FETCH_BUDGET_SEC", 6600))   # 110分
_RUN_STARTED = time.time()

# 同一ホストで通算これだけ弾かれたら、その実行ではもう叩かない。
# 休憩を挟んでも戻らない＝IP単位で拒否されている状態なので、待つだけ無駄。
HOST_GIVEUP_FAILS = int(os.environ.get("HOST_GIVEUP_FAILS", 12))
# ホストごとの見切り回数。実測(run 34394910814, 95分):
#   アットホーム … 弾かれ9回で打ち切り。成功0件。IPごと拒否されている
#   HOMES        … 弾かれ73回に対し成功7回。320秒休んでも復帰しない
# 以前は「HOMESは300秒休めば復帰する」が成立していたが、もう通らない。
# Actionsからこの2サイトを粘って取るのは待ち時間だけが増えるので浅く切る。
# （この2サイトはローカル実行だと普通に取れる。実測: HOMES32件/athome60件）
_HOST_GIVEUP = {"www.athome.co.jp": 3, "www.homes.co.jp": 4,
                "myhome.nifty.com": 8}
# 1ホストの休憩の累計上限(秒)。ここを超えたらそのホストは打ち切る。
# 見積もりでHOMESは休憩だけで208分に達し得るため、1サイトが実行時間を
# 食い潰さないように上限を設ける。
HOST_REST_CAP_SEC = int(os.environ.get("HOST_REST_CAP_SEC", 1800))   # 30分
_HOST_RESTED = {}
_HOST_DEAD = set()

# 在庫ページに載せた件数。次回の実行で「大きく減っていないか」を見るために
# state.json へ持ち越す。
_PAGE_COUNT = 0


def budget_left():
    return FETCH_BUDGET_SEC - (time.time() - _RUN_STARTED)

# bot検知が厳しいサイトは「同時1本 + 最低間隔」で叩く。
# 並列化した状態で普通に投げると即ブロックされるため。
_HOST_GATE = {
    "www.homes.co.jp":  (threading.Lock(), 4.0),
    "www.athome.co.jp": (threading.Lock(), 4.0),
    "www.rehouse.co.jp": (threading.Lock(), 1.5),
    # SUUMOは並列で叩くと503を連発する（実測157件）。同時1本+1.2秒間隔。
    "suumo.jp":         (threading.Lock(), 1.3),
    # 新しく追加した3サイト。まとめて叩くと弾かれるので同時1本+間隔を置く
    "myhome.nifty.com": (threading.Lock(), 2.5),
    "sumaity.com":      (threading.Lock(), 1.5),
    "www.chintai.net":  (threading.Lock(), 1.5),
    "www.housecom.jp":  (threading.Lock(), 1.5),
    "smocca.jp":        (threading.Lock(), 1.5),
    "cowcamo.jp":       (threading.Lock(), 1.5),
    "house.goo.ne.jp":  (threading.Lock(), 1.5),
}
_HOST_LAST = {}


# 503が出たら間隔を自動で広げる。ブロックの原因は通信方法ではなく
# 短時間のアクセス回数なので、詰まったら黙って遅くするのが一番効く。
_HOST_INTERVAL = {}
MAX_INTERVAL = 6.0

# HOMES/アットホームは「1実行あたり最初の5〜6回だけ通し、超えるとIPごと
# ブロックして間隔を空けても解けない」仕様（Actions上で実測）。
# 予算内に収め、最初の202が出た時点で打ち切る。
# HOMESはActionsのIPから連続5〜6リクエストで弾かれるが、
# 300秒待てば復帰する（2026-09-08実測: 60秒/120秒はNG、300秒でOK）。
# そこで「N件取ったら休む」方式にして全駅から取り切る。
# ニフティ不動産は実測で「まとめて叩くと405を返し、約120秒で復帰」。
# 22駅ぶんの調査(約24リクエスト)は通ったので枠は広め。
_HOST_QUOTA = {"www.homes.co.jp": 5, "www.athome.co.jp": 15,
               "myhome.nifty.com": 15}
_HOST_COOLDOWN_SEC = {"www.homes.co.jp": 320, "www.athome.co.jp": 180,
                      "myhome.nifty.com": 150}
_HOST_USED = {}
_HOST_BUDGET = {}   # 予算制は廃止（互換のため空で残す）


def take_slot(host):
    """N件使ったら所定時間休んでから続ける。休めば復帰するので諦めない。"""
    q = _HOST_QUOTA.get(host)
    if not q:
        return
    with _BUDGET_LOCK:
        used = _HOST_USED.get(host, 0) + 1
        _HOST_USED[host] = used
        need_rest = (used % q == 0)
    if need_rest:
        wait = _HOST_COOLDOWN_SEC.get(host, 300)
        with _BUDGET_LOCK:
            rested = _HOST_RESTED.get(host, 0)
        if rested + wait > HOST_REST_CAP_SEC:
            _HOST_DEAD.add(host)
            print(f"  {host}: 休憩の累計が上限({HOST_REST_CAP_SEC}秒)に達したため打ち切ります",
                  file=sys.stderr)
            return
        with _BUDGET_LOCK:
            _HOST_RESTED[host] = rested + wait
        if wait >= budget_left():
            # 休むと締切を割る。ここで待つより他のサイトに時間を回す。
            _HOST_DEAD.add(host)
            print(f"  {host}: 残り時間が足りないため以降の取得を打ち切ります",
                  file=sys.stderr)
            return
        print(f"  {host}: {used}件取得。ブロック回避のため{wait}秒休みます",
              file=sys.stderr)
        time.sleep(wait)
_BUDGET_LOCK = threading.Lock()


def consume_budget(host):
    """予算があれば1消費してTrue。無ければFalse（=叩かない）"""
    with _BUDGET_LOCK:
        if host not in _HOST_BUDGET:
            return True
        if _HOST_BUDGET[host] <= 0:
            return False
        _HOST_BUDGET[host] -= 1
        return True


# 連続で失敗した回数。単発の202で全部止めると取り逃すので、
# 連続3回失敗して初めて諦める
_HOST_FAILS = {}
CONSECUTIVE_FAIL_LIMIT = 3


def note_fail(host):
    with _BUDGET_LOCK:
        _HOST_FAILS[host] = _HOST_FAILS.get(host, 0) + 1
        return _HOST_FAILS[host]


def note_ok(host):
    with _BUDGET_LOCK:
        _HOST_FAILS[host] = 0


def kill_budget(host):
    with _BUDGET_LOCK:
        if host in _HOST_BUDGET:
            _HOST_BUDGET[host] = 0


def slow_down(host, reason=""):
    """そのホストへの間隔を広げる（上限あり）"""
    base = _HOST_GATE.get(host, (None, 1.0))[1]
    cur = _HOST_INTERVAL.get(host, base)
    new_i = min(MAX_INTERVAL, cur * 1.6 + 0.3)
    if new_i > cur:
        _HOST_INTERVAL[host] = new_i
        print(f"  {host}: 間隔を{cur:.1f}→{new_i:.1f}秒に広げます {reason}",
              file=sys.stderr)


def speed_up(host):
    """成功が続いたら少しずつ元に戻す"""
    base = _HOST_GATE.get(host, (None, 1.0))[1]
    cur = _HOST_INTERVAL.get(host)
    if cur and cur > base:
        _HOST_INTERVAL[host] = max(base, cur - 0.15)


class _Gate:
    """厳しいホストは直列化し、前回アクセスから最低interval秒あける"""

    def __init__(self, host):
        self.g = _HOST_GATE.get(host)
        self.host = host

    def __enter__(self):
        if self.g:
            lock, base = self.g
            lock.acquire()
            interval = _HOST_INTERVAL.get(self.host, base)
            wait = interval - (time.time() - _HOST_LAST.get(self.host, 0))
            if wait > 0:
                time.sleep(wait)
        return self

    def __exit__(self, *a):
        if self.g:
            _HOST_LAST[self.host] = time.time()
            self.g[0].release()


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else url


def fetch_with_retry(url: str, impersonate: bool = False, max_retry: int = 4):
    if "suumo.jp" in url:
        max_retry = 2
    """bot検知(HOMES 202/athome認証中)対策: リトライ+指数バックオフ+ホスト単位クールダウン"""
    host = _host_of(url)
    if budget_left() <= 0:
        return ""            # 取得の締切切れ。残りは諦めて公開まで進む
    if host in _HOST_DEAD:
        return ""            # このホストはこの実行では見捨てた
    with _BLOCK_LOCK:
        until = _BLOCKED_UNTIL.get(host, 0)
    if time.time() < until:
        return ""  # ブロック中。叩かない
    take_slot(host)
    for attempt in range(max_retry):
        with _Gate(host):
            html = fetch(url, impersonate=impersonate)
        if html is _NOT_FOUND or html == _NOT_FOUND:
            return ""      # ページが無いだけ。減速もブロック判定もしない
        # HOMESの202はfetch内でhtml=""になる。athomeの認証中ページは硬い200なので中身で判定
        if html and "認証中" not in html[:3000]:
            speed_up(host)
            note_ok(host)
            return html
        if host in _HOST_QUOTA:
            # 弾かれたら休んで再挑戦する。300秒で復帰することを実測済み。
            n = note_fail(host)
            if n >= _HOST_GIVEUP.get(host, HOST_GIVEUP_FAILS):
                # 休憩を挟んでも戻らない＝IPごと拒否されている。待つほど
                # 他のサイトの取得時間を削るだけなので、以降は叩かない。
                _HOST_DEAD.add(host)
                print(f"  {host}: 通算{n}回弾かれたため、この実行では取得を打ち切ります",
                      file=sys.stderr)
                return ""
            if attempt == max_retry - 1:
                return ""
            wait = _HOST_COOLDOWN_SEC.get(host, 300)
            if wait >= budget_left():
                _HOST_DEAD.add(host)
                print(f"  {host}: 休憩{wait}秒ぶんの時間が残っていないため打ち切ります",
                      file=sys.stderr)
                return ""
            print(f"  {host}: 弾かれたため{wait}秒休んで再試行 ({n}回目)",
                  file=sys.stderr)
            time.sleep(wait)
            continue
        slow_down(host, f"(retry {attempt + 1}/{max_retry})")
        if attempt == max_retry - 1:
            break
        # SUUMOの503は待てば直る類ではなく、単に混んでいるだけのことが多い。
        # 指数バックオフを長く取ると実行時間が跳ね上がる（実測: 待ち時間だけで27分）
        if host == "suumo.jp":
            wait = 2.5 * (attempt + 1) + random.uniform(0, 1.5)
        else:
            wait = 6 * (2 ** attempt) + random.uniform(0, 4)
        time.sleep(wait)
    # 規定回数失敗 → このホストはしばらく諦める
    # ただしSUUMOは本命ソースなので諦めない（諦めるとプールが激減する）
    if host == "suumo.jp":
        return ""
    with _BLOCK_LOCK:
        _BLOCKED_UNTIL[host] = time.time() + BLOCK_COOLDOWN
    print(f"  {host} をbot検知と判断し{BLOCK_COOLDOWN}秒スキップ", file=sys.stderr)
    return ""


def homes_stations_today(n=2):
    """HOMESは1実行6リクエスト(=2駅×3種別)しか通らない。
    日替わりで対象駅をずらし、数日かければ全駅を回れるようにする。
    第一希望と優先駅を先頭に置いた順で巡回する。
    """
    from datetime import datetime, timezone, timedelta
    # 第一希望の大井町は毎日固定。残り1枠を他の駅で日替わりに回す。
    fixed = "大井町" if STATIONS.get("大井町", {}).get("homes") else None
    rest = [k for k in (tuple(PRIORITY_STATIONS)
                        + tuple(x for x in STATIONS if x not in PRIORITY_STATIONS))
            if k != fixed and STATIONS.get(k, {}).get("homes")]
    if not rest:
        return (fixed,) if fixed else ()
    doy = datetime.now(timezone(timedelta(hours=9))).timetuple().tm_yday
    picked = [rest[(doy + i) % len(rest)] for i in range(max(0, n - (1 if fixed else 0)))]
    return tuple(([fixed] if fixed else []) + picked)


def collect_all():
    """駅ごとの取得を並列実行。同一サイトへの同時接続は WORKERS で抑える。"""
    all_items = []
    portal_count = Counter()

    global HOMES_TODAY
    print(f"HOMES: 全{sum(1 for v in STATIONS.values() if v.get('homes'))}駅から取得"
          f"（{_HOST_QUOTA['www.homes.co.jp']}件ごとに"
          f"{_HOST_COOLDOWN_SEC['www.homes.co.jp']}秒休憩）")

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        # HOMES/アットホームは1実行あたりのリクエスト数に上限があるので、
        # 予算が尽きる前に優先駅と第一希望を先に回す
        _head = tuple(PRIORITY_STATIONS) + ("大井町", "戸越", "武蔵小山")
        _ordered = sorted(STATIONS.items(),
                          key=lambda kv: (_head.index(kv[0]) if kv[0] in _head else 99, kv[0]))
        futs = {ex.submit(collect_station, st, cd): st for st, cd in _ordered}
        for f in as_completed(futs):
            st = futs[f]
            try:
                items, counts, log = f.result()
                all_items.extend(items)
                portal_count.update(counts)
                results.append((st, log))
            except Exception as e:
                print(f"[{st}] 取得失敗: {e}", file=sys.stderr)
    for st, log in sorted(results):
        for line in log:
            print(line)

    print("\n=== ポータル別取得 ===")
    for k, v in portal_count.most_common():
        print(f"  {k}: {v}")

    return all_items


HOMES_TODAY = ()


def collect_station(station, codes):
    """1駅ぶんを取得して (items, portal_count, ログ行) を返す。"""
    all_items = []
    portal_count = Counter()
    log = []
    if True:
        # SUUMO (5種別: 中古3 + 新築2)
        pref = codes.get("pref", "tokyo")   # 駅ごとに都道府県を指定（既定は東京）
        for kind, path in [("mansion", f"ms/chuko/{pref}/ek_{codes['suumo']}/"),
                            ("house",   f"chukoikkodate/{pref}/ek_{codes['suumo']}/"),
                            ("land",    f"tochi/{pref}/ek_{codes['suumo']}/"),
                            # 新築戸建のみ追加。新築マンションは実測でほぼ通らず
                            # アクセス数だけ増えるので外す
                            ("house",   f"ikkodate/{pref}/ek_{codes['suumo']}/")]:
            # 恵比寿・広尾・代官山などは条件を満たす物件が極端に少ない。
            # 実測（恵比寿172件/広尾170件/代官山180件を全走査）で通過は1件だけだった。
            # 取りこぼしを無くすため、この駅だけ深いページまで見る。
            # 件数を増やすため深く見る（浅いと候補を取りこぼす）
            pages = tuple(range(1, 11)) if station in DEEP_SCAN_STATIONS else tuple(range(1, 8))
            items = []
            for pn in pages:
                # mb で面積下限をサーバー側に渡す（実測: 大井町の中古マンションで
                # 50㎡未満14件→0件。同じ取得回数でも大きい物件を多く拾える）
                # 土地は建物面積の概念が違うので mb は付けない
                _mb = "" if kind == "land" else f"&mb={suumo_mb(AREA_MIN)}"
                url = (f"https://suumo.jp/{path}?et=10&pn={pn}"
                       f"&kb={PRICE_MIN}&kt={PRICE_MAX}{_mb}")
                html = fetch_with_retry(url)
                page_items = parse_suumo(html, station, kind)
                if not page_items:
                    break  # ページ切れ
                items.extend(page_items)
                time.sleep(SLEEP_BETWEEN)
            kept = filter_with_walk_rescue(items)
            log.append(f"[SUUMO {kind}] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count[f"SUUMO {kind}"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # HOMES (3種別) — Cloudflare回避でcurl_cffi使用 + リトライ
        # 駅コード未検証のポータルはスキップ（推測URLで別エリアを拾わないため）
        # 予算5回に収める。マンションはSUUMO/ノムコムで足りているが、
        # 土地は掲載自体が少ないので、この枠は土地に使う。
        # 休憩を挟めば全駅から取れる
        use_homes = bool(codes.get("homes"))
        for kind, path in ([] if not use_homes else
                           [("mansion", f"mansion/chuko/{pref}/{codes['homes']}/list/"),
                            ("house",   f"kodate/chuko/{pref}/{codes['homes']}/list/"),
                            ("land",    f"tochi/{pref}/{codes['homes']}/list/")]):
            items = []
            for pn in (1, 2, 3):   # 休憩を挟むので枠内で深く取る
                url = f"https://www.homes.co.jp/{path}?page={pn}"
                html = fetch_with_retry(url, impersonate=True)
                page_items = parse_homes(html, station, kind)
                if not page_items:
                    break
                items.extend(page_items)
            kept = filter_with_walk_rescue(items)
            log.append(f"[HOMES {kind}] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count[f"HOMES {kind}"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # HOMESの賃貸 — 売買と同じ駅スラッグが使える。一覧に階・敷礼・
        # 築年数まで載る（実測: 目黒66件・主要項目61/66）
        if use_homes:
            items = []
            for pn in (1, 2, 3):
                url = (f"https://www.homes.co.jp/chintai/{pref}/"
                       f"{codes['homes']}/list/?page={pn}")
                html = fetch_with_retry(url, impersonate=True)
                page_items = parse_homes_rent(html, station)
                if not page_items:
                    break
                items.extend(page_items)
                time.sleep(SLEEP_BETWEEN)
            kept = [i for i in items if apply_rent_filters(i)]
            log.append(f"[HOMES賃貸] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count["HOMES賃貸"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # アットホーム (3種別) — Cloudflare回避でcurl_cffi使用 + リトライ
        use_athome = bool(codes.get("athome"))
        for kind, path in ([] if not use_athome else
                           [("mansion", f"mansion/chuko/{pref}/{codes['athome']}/list/"),
                            ("house",   f"kodate/{pref}/{codes['athome']}/list/"),
                            ("land",    f"tochi/{pref}/{codes['athome']}/list/"),
                            ("rent",    f"chintai/{pref}/{codes['athome']}/list/")]):
            items = []
            for pn in (1, 2, 3):   # 休憩を挟むので枠内で深く取る
                url = f"https://www.athome.co.jp/{path}?page={pn}"
                html = fetch_with_retry(url, impersonate=True)
                page_items = (parse_athome_rent(html, station) if kind == "rent"
                              else parse_athome(html, station, kind))
                if not page_items:
                    break
                items.extend(page_items)
            kept = ([i for i in items if apply_rent_filters(i)] if kind == "rent"
                    else filter_with_walk_rescue(items))
            log.append(f"[アットホーム {kind}] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count[f"アットホーム {kind}"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # goo住宅・不動産の中古マンション。戸建・新築は同じページ構造でも
        # 面積や築年が欄に出ず主要項目が埋まらなかった（実測）ので入れない
        if codes.get("nomu"):
            _bp, _bl, _bc = codes["nomu"].split("/")
            # 中古マンション/中古戸建/新築マンションを取る。
            # 中古戸建は table.tab.property 全体が1物件で、建物面積・築年月・
            # 複数駅の徒歩まで入る（実測: 目黒40件・主要項目40/40）
            for _seg, _kind, _lbl in [("um", "mansion", "mansion"),
                                      ("uh", "house", "house"),
                                      ("bm", "mansion", "mansion")]:
                items = []
                for pn in range(1, 9):
                    url = (f"https://house.goo.ne.jp/buy/shuto_{_seg}/ensen/"
                           f"{_bl[1:]}/{_bc}.html"
                           + ("" if pn == 1 else f"?p={pn}"))
                    html = fetch_with_retry(url, impersonate=True)
                    page_items = parse_goo_buy(html, station, _kind)
                    if not page_items:
                        break
                    items.extend(page_items)
                    time.sleep(SLEEP_BETWEEN)
                kept = filter_with_walk_rescue(items)
                log.append(f"[goo売買 {_seg}] {station}: parsed={len(items)} kept={len(kept)}")
                all_items.extend(kept)
                portal_count[f"goo住宅 {_lbl}"] += len(kept)
                time.sleep(SLEEP_BETWEEN)

        # goo住宅・不動産の賃貸 — 駅コードはノムコムと同じ体系。
        # 路線だけ先頭1桁を落とす（2172→172）。実測: 目黒70件・蒲田108件
        if codes.get("nomu"):
            _gp, _gl, _gc = codes["nomu"].split("/")
            items = []
            # 実測: 3〜6ページ目でも 52/64/182/82件と別物件が出続ける
            for pn in range(1, 9):
                # ページ送りは ?p=N（?page=は無視される。実測で確認）
                url = (f"https://house.goo.ne.jp/rent/shuto_ap/ensen/"
                       f"{_gl[1:]}/{_gc}.html"
                       + ("" if pn == 1 else f"?p={pn}"))
                html = fetch_with_retry(url, impersonate=True)
                page_items = parse_goo(html, station)
                if not page_items:
                    break
                items.extend(page_items)
                time.sleep(SLEEP_BETWEEN)
            kept = [i for i in items if apply_rent_filters(i)]
            log.append(f"[goo住宅] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count["goo住宅"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # 賃貸スモッカ — 駅コードはノムコム(国交省体系)をそのまま使える。
        # 蛍池はノムコム未設定なのでリハウスのコードから導く（22駅で実測確認）
        _sm = None
        if codes.get("nomu"):
            _p, _l, _c = codes["nomu"].split("/")
            _sm = ("tokyo", _l, _c)
        elif codes.get("rehouse"):
            _pf, _rw, _st = codes["rehouse"].split("/")
            _sm = ("osaka" if _pf == "27" else "tokyo", _rw, _rw + _st)
        if _sm:
            _sp, _sl, _sc = _sm
            items = []
            for pn in range(1, 6):
                # ページ送りは ?page= ではなく /page/N（?page=は無視され
                # 1ページ目が返る。実測で確認）
                url = (f"https://smocca.jp/search/{_sp}/line/{_sl}/station/{_sc}"
                       + ("" if pn == 1 else f"/page/{pn}"))
                html = fetch_with_retry(url, impersonate=True)
                page_items = parse_smocca(html, station)
                if not page_items:
                    break
                items.extend(page_items)
                time.sleep(SLEEP_BETWEEN)
            kept = [i for i in items if apply_rent_filters(i)]
            log.append(f"[スモッカ] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count["賃貸スモッカ"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # カウカモ（中古マンション・リノベ）— 一覧に築年が出ないので
        # 他条件を満たすものだけ詳細ページから築年を補完する
        if codes.get("cowcamo"):
            items = []
            for pn in range(1, 4):
                url = (f"https://cowcamo.jp/station/{codes['cowcamo']}"
                       + ("" if pn == 1 else f"?page={pn}"))
                html = fetch_with_retry(url, impersonate=True)
                page_items = parse_cowcamo(html, station)
                if not page_items:
                    break
                items.extend(page_items)
                time.sleep(SLEEP_BETWEEN)
            kept = filter_with_walk_rescue(items)
            log.append(f"[カウカモ] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count["カウカモ"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # スマイティの賃貸 — 1駅で260件超が取れる最大級の供給源（実測）
        if codes.get("sumaity_rent"):
            _sp, _ss = codes["sumaity_rent"].split("/", 1)
            items = []
            # 実測: 4〜7ページ目でも 113/85/74/90件と別物件が出続ける
            for pn in range(1, 11):
                url = (f"https://sumaity.com/chintai/{_sp}_eki/{_ss}-eki/"
                       + ("" if pn == 1 else f"?page={pn}"))
                html = fetch_with_retry(url, impersonate=True)
                page_items = parse_sumaity_rent(html, station)
                if not page_items:
                    break
                items.extend(page_items)
                time.sleep(SLEEP_BETWEEN)
            kept = [i for i in items if apply_rent_filters(i)]
            log.append(f"[スマイティ賃貸] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count["スマイティ賃貸"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # 東急リバブルの賃貸 — 売買と同じ駅コードが使える。一覧に
        # 所在階・敷礼・築年月まで載る（実測: 目黒で30件・欠損0）
        if codes.get("livable"):
            items = []
            for pn in range(1, 6):
                url = (f"https://www.livable.co.jp/chintai/{codes['livable']}/"
                       + ("" if pn == 1 else f"?page={pn}"))
                html = fetch_with_retry(url)
                page_items = parse_livable_rent(html, station)
                if not page_items:
                    break
                items.extend(page_items)
                time.sleep(SLEEP_BETWEEN)
            kept = [i for i in items if apply_rent_filters(i)]
            log.append(f"[リバブル賃貸] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count["リバブル賃貸"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # ハウスコム(賃貸) — 建物ごとに部屋が並び、階・敷礼・築年まで一覧に載る
        if codes.get("housecom"):
            _hp, _hc = codes["housecom"].split("/")
            items = []
            for pn in range(1, 6):
                url = (f"https://www.housecom.jp/{_hp}/{_hc}-st/"
                       + ("" if pn == 1 else f"?page={pn}"))
                html = fetch_with_retry(url, impersonate=True)
                page_items = parse_housecom(html, station)
                if not page_items:
                    break
                items.extend(page_items)
                time.sleep(SLEEP_BETWEEN)
            kept = [i for i in items if apply_rent_filters(i)]
            log.append(f"[ハウスコム] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count["ハウスコム"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # ニフティ不動産(横断検索) — SUUMO/HOMES/アットホーム等の在庫が入る。
        # ActionsからHOMES/アットホームを直接叩けない分をここで補う。
        if codes.get("nifty"):
            # 新築も取る。sho条件の「築20年未満」に確実に合致する上、
            # 中古だけだと候補が足りない（実測: 新築戸建 目黒で19件）
            for kind, path in [("mansion", f"chuko-mansion/{pref}/{codes['nifty']}_st/"),
                               ("house",   f"chuko-ikkodate/{pref}/{codes['nifty']}_st/"),
                               ("land",    f"tochi/{pref}/{codes['nifty']}_st/"),
                               ("rent",    f"rent/{pref}/{codes['nifty']}_st/"),
                               ("mansion", f"shinchiku-mansion/{pref}/{codes['nifty']}_st/"),
                               ("house",   f"shinchiku-ikkodate/{pref}/{codes['nifty']}_st/")]:
                items = []
                # ニフティは6種別あり、枠15件ごとに150秒の休憩が入るので
                # ページを深くしすぎない。休憩の累計上限(30分)もあるので3で打つ
                for pn in (1, 2, 3):
                    # ページ送りはクエリではなくパス末尾に /N/（実測。
                    # ?page= は無視され1ページ目が返っていた）
                    url = (f"https://myhome.nifty.com/{path}" if pn == 1
                           else f"https://myhome.nifty.com/{path}{pn}/")
                    html = fetch_with_retry(url, impersonate=True)
                    page_items = parse_nifty(html, station, kind)
                    if not page_items:
                        break
                    items.extend(page_items)
                    time.sleep(SLEEP_BETWEEN)
                kept = ([i for i in items if apply_rent_filters(i)] if kind == "rent"
                        else filter_with_walk_rescue(items))
                log.append(f"[ニフティ {kind}] {station}: parsed={len(items)} kept={len(kept)}")
                all_items.extend(kept)
                portal_count[f"ニフティ {kind}"] += len(kept)
                time.sleep(SLEEP_BETWEEN)

        # CHINTAI(賃貸) — 一覧に所在階・敷金礼金・築年まで載っている
        if codes.get("chintai"):
            _pf, _sc = codes["chintai"].split("/")
            items = []
            # ページ送りは ?page= ではなく /list/pageN/（実測。?page=は無視され
            # 同じ1ページ目が返るため重複していた）
            # 実測: 6/8/10/12ページ目でも 55/50/38/38件と別物件が出続ける
            pages = tuple(range(1, 21)) if station in DEEP_SCAN_STATIONS else tuple(range(1, 15))
            # 賃貸マンション/アパートに加えて賃貸戸建(list/kodate/)も取る。
            # 戸建は面積が広く45㎡以上の条件に合いやすい（実測: 目黒16件）
            for sub in ("", "kodate/"):
                for pn in pages:
                    url = (f"https://www.chintai.net/{_pf}/ensen/{_sc}/list/{sub}"
                           + ("" if pn == 1 else f"page{pn}/"))
                    html = fetch_with_retry(url, impersonate=True)
                    page_items = parse_chintai(html, station)
                    if not page_items:
                        break
                    items.extend(page_items)
                    time.sleep(SLEEP_BETWEEN)
            kept = [i for i in items if apply_rent_filters(i)]
            log.append(f"[CHINTAI] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count["CHINTAI"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # スマイティ — 一覧に所在階まで載っているので詳細を見ずに済む
        if codes.get("sumaity"):
            for kind, path in [("mansion", f"mansion/used/{pref}/{codes['sumaity']}-eki/"),
                               ("house",   f"house/used/{pref}/{codes['sumaity']}-eki/")]:
                items = []
                # 実測: 目黒の中古マンションは9ページ目まで別物件が出続ける
                for pn in range(1, 11):
                    url = (f"https://sumaity.com/{path}" if pn == 1
                           else f"https://sumaity.com/{path}?page={pn}")
                    html = fetch_with_retry(url, impersonate=True)
                    page_items = parse_sumaity(html, station, kind)
                    if not page_items:
                        break
                    items.extend(page_items)
                    time.sleep(SLEEP_BETWEEN)
                kept = filter_with_walk_rescue(items)
                log.append(f"[スマイティ {kind}] {station}: parsed={len(items)} kept={len(kept)}")
                all_items.extend(kept)
                portal_count[f"スマイティ {kind}"] += len(kept)
                time.sleep(SLEEP_BETWEEN)

        # 三井のリハウス — all-type で全種別を1URLから取得
        if codes.get("rehouse"):
            items = []
            _pf, _rw, _st = codes["rehouse"].split("/")
            # all-type は中古3種のみ。新築マンション/新築戸建は別URLなので
            # 追加で取る（実測: 新築戸建 目黒で3件、築2026年）
            # all-type だけだと種別ごとの在庫を取りこぼす。
            # 実測(目黒): all-type 30件だけ → 全種別を回すと51件
            #   tochi +5 / kodate +9 / mansion +4 / s_kodate +3
            for _seg in ("all-type", "mansion", "kodate", "tochi",
                         "s_mansion", "s_kodate"):
                base = (f"https://www.rehouse.co.jp/buy/{_seg}/prefecture/"
                        f"{_pf}/railway/{_rw}/station/{_st}/")
                # all-type は在庫が厚いので深く、種別別は1〜2ページで尽きる
                # （実測: tochi 8件・kodate 13件・s_kodate 3件）
                _last = 11 if _seg == "all-type" else 3
                for pn in range(1, _last):
                    url = base if pn == 1 else f"{base}?page={pn}"
                    html = fetch_with_retry(url, impersonate=True)
                    page_items = parse_rehouse(html, station)
                    if not page_items:
                        break
                    items.extend(page_items)
                    if len(page_items) < 30:
                        break          # 最終ページ
                    time.sleep(SLEEP_BETWEEN)
            kept = filter_with_walk_rescue(items)
            log.append(f"[リハウス] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count["三井のリハウス"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # ノムコム
        for kind, path in ([] if not codes.get("nomu") else
                           [("mansion", f"mansion/{codes['nomu']}/"),
                            ("house",   f"house/{codes['nomu']}/"),
                            ("land",    f"land/{codes['nomu']}/")]):
            items = []
            # ページ送りは ?page= ではなく ?pager_page=（?page=は無視され
            # 1ページ目が返っていた。実測で確認）
            for pn in range(1, 7):
                url = f"https://www.nomu.com/{path}?pager_page={pn}"
                html = fetch(url)
                page_items = parse_nomu(html, station, kind)
                if not page_items:
                    break
                items.extend(page_items)
                time.sleep(1.0)
            kept = filter_with_walk_rescue(items)
            log.append(f"[ノムコム {kind}] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count[f"ノムコム {kind}"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # リバブル
        for kind, path in ([] if not codes.get("livable") else
                           [("mansion", f"kounyu/mansion/{codes['livable']}/"),
                            ("house",   f"kounyu/kodate/{codes['livable']}/"),
                            ("land",    f"kounyu/tochi/{codes['livable']}/")]):
            items = []
            for pn in range(1, 7):
                url = f"https://www.livable.co.jp/{path}?page={pn}"
                html = fetch(url)
                page_items = parse_livable(html, station, kind)
                if not page_items:
                    break
                items.extend(page_items)
                time.sleep(1.0)
            kept = filter_with_walk_rescue(items)
            log.append(f"[リバブル {kind}] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count[f"リバブル {kind}"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # SUUMO 賃貸（管理費込みRENT_MAX以下）— 2ページまで
        rent_items = []
        for pn in range(1, 7):   # 賃貸は最大の供給源なので深く取る
            # 賃貸もサーバー側で面積と賃料を絞る（実測: 大井町1ページで
            # 45㎡未満が58件→0件。取得枠を狭い部屋に食われなくなる）
            # bs=040 は賃貸戸建。マンション/アパート枠だけだと戸建を
            # 取りこぼす（実測: 目黒で38件出た）。両方まわす
            for _bs in ("", "&ar=030&bs=040"):
                url = (f"https://suumo.jp/chintai/{codes.get('pref', 'tokyo')}"
                       f"/ek_{codes['suumo']}/?page={pn}"
                       f"&mb={suumo_mb(RENT_AREA_MIN)}&cb={RENT_MIN}&ct={RENT_MAX}"
                       f"{_bs}")
                html = fetch_with_retry(url)
                page = parse_suumo_rent(html, station)
                if page:
                    rent_items.extend(page)
                time.sleep(SLEEP_BETWEEN)
            if not page:
                break
        keep_raw(rent_items)
        kept = [it for it in rent_items if apply_rent_filters(it)]
        # 同一物件の重複部屋を間引き（住所+賃料+面積+間取りで一意化）
        # ※建物名が「◯◯マンション」と「品川区◯◯ 賃貸」で割れても同一とみなす
        # 同一建物の別部屋は「間取り or 賃料 or 面積」が違えば残す。
        # 全部同じ部屋は重複とみなして捨てる。残す場合は何が違うかを注記する。
        seen_key = set()
        per_building = {}
        uniq = []
        # 実名の物件を優先し、安い順に見る
        for it in sorted(kept, key=lambda x: (0 if not x["name"].endswith("賃貸") else 1,
                                              x.get("price") or 999)):
            bkey = it.get("addr") or it.get("name")
            # 間取り・賃料・面積が全部一致 → 同じ部屋の重複掲載。捨てる
            k = (bkey, it.get("layout"), it.get("price"), it.get("area"))
            if k in seen_key:
                continue
            siblings = per_building.setdefault(bkey, [])
            if len(siblings) >= MAX_ROOMS_PER_BUILDING:
                continue   # 別部屋でも同じ建物ばかりで枠を埋めない
            seen_key.add(k)
            if siblings:
                # 同じ建物の既出物件と何が違うのかを書く
                ref = siblings[0]
                diffs = []
                if it.get("floor") and it.get("floor") != ref.get("floor"):
                    diffs.append(f"{it['floor']}")
                if it.get("layout") and it.get("layout") != ref.get("layout"):
                    diffs.append(f"{it['layout']}")
                if it.get("area") and it.get("area") != ref.get("area"):
                    diffs.append(f"{it['area']}㎡")
                if it.get("price") and it.get("price") != ref.get("price"):
                    if it.get("type") == "rent":
                        diffs.append(f"{it['price']}万円/月")
                    else:
                        diffs.append(fmt_price_man(it.get("price")))
                label = "・".join(diffs) if diffs else "別部屋"
                it["_dup_note"] = f"※同じ建物の別部屋({label})"
            siblings.append(it)
            uniq.append(it)
        log.append(f"[SUUMO賃貸] {station}: parsed={len(rent_items)} kept={len(uniq)}")
        all_items.extend(uniq)
        portal_count["SUUMO賃貸"] += len(uniq)
        time.sleep(SLEEP_BETWEEN)

    return all_items, portal_count, log


# === フィルタ ===

def filter_with_walk_rescue(items):
    """フィルタ適用。walk=Noneでも他条件を満たす物件は詳細ページからwalkを取得して救済"""
    keep_raw(items)
    kept = []
    for it in items:
        if apply_filters(it):
            kept.append(it)
            continue
        # walk か addr が欠損 & 他条件OK → 詳細ページから補完して再判定
        # addr は駅ごとの区チェックに必須。欠損のまま通すと一覧の広告枠を拾う。
        needs_walk = it.get("walk") is None
        needs_addr = not it.get("addr") and STATION_AREAS.get(it.get("station"))
        # 築年が一覧に出ないサイト（カウカモ等）は築年も詳細から補う。
        # 補わないと「築年不明」で全件落ちる（実測: カウカモが全駅0件だった）
        needs_built = (not it.get("built")
                       and it.get("type") in ("mansion", "house", "rent"))
        if (needs_walk or needs_addr or needs_built) and passes_except_walk(it):
            use_cffi = it["source"] in ("HOMES", "アットホーム", "三井のリハウス",
                                        "スマイティ", "CHINTAI", "ニフティ不動産",
                                        "ハウスコム", "賃貸スモッカ", "goo住宅",
                                        "カウカモ")
            html = fetch(it["url"], impersonate=use_cffi)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text(" ", strip=True)
                if needs_walk:
                    w = parse_walk(text, it["station"])
                    if w is not None:
                        it["walk"] = w
                if needs_built:
                    row = _row_value(soup, "完成時期", "築年月", "建築年月",
                                     "竣工", "築年数", "築年")
                    b = parse_built(row) if row else None
                    if not b:
                        mb2 = re.search(r"(\d{4})年\s*\d{0,2}月?\s*築|築年[^0-9]{0,4}(\d{4})年", text)
                        if mb2:
                            b = int(mb2.group(1) or mb2.group(2))
                    if not b:
                        # 「築年月 築55年」のように築年数で書くサイトがある
                        mb3 = re.search(r"築年月?\s*築\s*(\d{1,3})\s*年", text)
                        if mb3:
                            b = CURRENT_YEAR - int(mb3.group(1))
                    if b:
                        it["built"] = b
                if needs_addr:
                    a = parse_addr(text) if "parse_addr" in globals() else None
                    if a:
                        it["addr"] = a
                    else:
                        m = re.search(r"(東京都[^\s、,]{1,4}区[^\s、,]{0,12})", text)
                        if m:
                            it["addr"] = m.group(1)
                if apply_filters(it):
                    kept.append(it)
            time.sleep(1.0)
    return kept


def apply_rent_filters(item):
    """賃貸用フィルタ: 管理費込み賃料・面積・徒歩"""
    p = item.get("price")
    if p is None or p < RENT_MIN or p > RENT_MAX:
        return False
    a = item.get("area")
    if a is None or a < RENT_AREA_MIN:
        return False
    w = item.get("walk")
    rlimit = WALK_MAX_BY_STATION.get(item.get("station"), RENT_WALK_MAX)
    if w is None or w > rlimit:
        return False
    # 築20年未満のみ。築年が取れない物件は条件を検証できないので通さない
    b = item.get("built")
    if not b or (CURRENT_YEAR - b) >= RENT_MAX_AGE:
        return False
    # 駅ごとの許容エリア
    addr = item.get("addr", "")
    allowed = STATION_AREAS.get(item.get("station"))
    if allowed:
        if not addr:
            return False
        if not any(a in addr for a in allowed):
            return False
    return True


def passes_except_walk(item):
    """walk以外の条件判定。walk=Noneの物件を詳細fetchすべきか決めるために使う"""
    price = item.get("price")
    if price is None or price < PRICE_MIN or price > PRICE_MAX:
        return False
    area = item.get("area")
    if area is None or area < AREA_MIN:
        return False
    if item.get("type") in ("mansion", "house"):
        layout = item.get("layout", "")
        if re.match(r"^1[LDKR]+$", layout):
            return False
    return True


# 落とした理由を数える。どの条件が効いているかを毎回の実行で把握するため。
REJECT_TALLY = Counter()
_TALLY_LOCK = threading.Lock()
# フィルタ前の生データ（条件を緩めた場合の試算に使う）
_RAW_FOR_WHATIF = []


def keep_raw(items):
    with _TALLY_LOCK:
        _RAW_FOR_WHATIF.extend(items)


def note_reject(reason):
    with _TALLY_LOCK:
        REJECT_TALLY[reason] += 1


def whatif_report(raw_items, current_count):
    """条件を1つだけ緩めたら何件になるかを試算する。
    「落とした理由」は最初に引っかかった条件しか数えないので、
    実際にどれだけ増えるかはこちらで測る。追加のアクセスは不要。
    """
    import copy

    def count(**over):
        keys = ("WALK_MAX", "AREA_MIN", "MANSION_MAX_AGE", "HOUSE_MAX_AGE",
                "RENT_MAX_AGE", "PRICE_MAX", "RENT_AREA_MIN", "RENT_MAX",
                "RENT_WALK_MAX")
        saved = {k: globals()[k] for k in keys}
        wsaved = dict(WALK_MAX_BY_STATION)
        try:
            for k, v in over.items():
                if k == "_walk_by_station":
                    WALK_MAX_BY_STATION.clear()
                    WALK_MAX_BY_STATION.update(v)
                else:
                    globals()[k] = v
            n = 0
            for it in raw_items:
                t = it.get("type")
                n += bool(apply_rent_filters(it) if t == "rent" else apply_filters(it))
            return n
        finally:
            for k, v in saved.items():
                globals()[k] = v
            WALK_MAX_BY_STATION.clear()
            WALK_MAX_BY_STATION.update(wsaved)

    base = count()
    rows = [
        ("徒歩を10分までにする", dict(WALK_MAX=10, RENT_WALK_MAX=10,
                                _walk_by_station={k: 10 for k in WALK_MAX_BY_STATION})),
        ("徒歩を8分までにする", dict(WALK_MAX=8, RENT_WALK_MAX=8)),
        ("築年を25年未満にする", dict(MANSION_MAX_AGE=25, HOUSE_MAX_AGE=25, RENT_MAX_AGE=25)),
        ("築年を30年未満にする", dict(MANSION_MAX_AGE=30, HOUSE_MAX_AGE=30, RENT_MAX_AGE=30)),
        ("面積を40㎡以上にする", dict(AREA_MIN=40.0)),
        ("価格を1.5億までにする", dict(PRICE_MAX=15000)),
        ("賃料を32万までにする", dict(RENT_MAX=32.0)),
    ]
    print(f"\n=== 条件を1つ緩めた場合の試算（今は{base}件） ===")
    for label, over in rows:
        n = count(**over)
        print(f"  {label:<24} {n:>4}件 ({n - base:+d})")


def print_reject_tally():
    if not REJECT_TALLY:
        return
    total = sum(REJECT_TALLY.values())
    print(f"\n=== 条件で落とした {total}件の内訳 ===")
    for k, v in REJECT_TALLY.most_common(12):
        print(f"  {k}: {v}件")


def apply_filters(item):
    walk = item.get("walk")
    limit = WALK_MAX_BY_STATION.get(item.get("station"), WALK_MAX)
    if walk is None:
        note_reject("徒歩が不明"); return False
    if walk > limit:
        note_reject(f"徒歩{limit}分超"); return False

    price = item.get("price")
    if price is None:
        note_reject("価格が不明"); return False
    if price > PRICE_MAX:
        note_reject(f"価格が{PRICE_MAX}万超"); return False
    if price < PRICE_MIN:
        note_reject(f"価格が{PRICE_MIN}万未満"); return False

    kind = item.get("type")
    area = item.get("area")
    if area is None:
        note_reject("面積が不明"); return False
    if area < AREA_MIN:
        note_reject(f"面積が{AREA_MIN}㎡未満"); return False

    if kind in ("mansion", "house"):
        layout = item.get("layout", "")
        # 1LDK/1DK/1Kは除外。1SLDKや+S付きはOK
        if re.match(r"^1[LDKR]+$", layout):
            note_reject("間取りが1LDK/1DK/1K"); return False
    # 戸建・マンションは築20年未満のみ。
    # 築年が取れない物件は「築20年以下」を検証できないので通さない。
    # （通すと築50年の物件が「築年記載なし」として紛れ込む。実際に発生した）
    if kind in ("house", "mansion"):
        b = item.get("built")
        limit = HOUSE_MAX_AGE if kind == "house" else MANSION_MAX_AGE
        if not b:
            note_reject("築年が不明"); return False
        if (CURRENT_YEAR - b) >= limit:
            note_reject(f"築{limit}年以上"); return False
        # 築年フィルタは無効化 (built値はノートとして残す)

    # 東京都内のみ許容（神奈川/埼玉/千葉等を除外）
    addr = item.get("addr", "")
    if addr and not re.search(r"(品川区|渋谷区|目黒区|港区|大田区|新宿区|世田谷区|中央区|台東区|豊中市|池田市)", addr):
        # 23区名がaddrにない → 都外の可能性。空addrは保留(parseミス)
        if "県" in addr or "市" in addr.replace("品川市", ""):
            return False

    # 駅ごとの許容エリア（明らかに遠い区の物件を弾く）
    allowed = STATION_AREAS.get(item.get("station"))
    if allowed:
        if not addr:
            # 住所不明だと区を検証できない＝一覧の広告枠を通してしまう。除外する。
            return False
        if not any(a in addr for a in allowed):
            return False

    if item["station"] == "大井町":
        for pat in OIMACHI_REJECT_PATTERNS:
            if pat in addr:
                return False

    return True


# === 永続化 ===

def is_watch_candidate(item):
    """予算オーバーだが価格以外は条件を満たす物件か。
    値下がりで予算内に入ってきたら知りたいので、捨てずに追跡する。
    """
    t = item.get("type")
    p = item.get("price")
    if p is None:
        return False
    if t == "rent":
        if not (RENT_MAX < p <= WATCH_RENT_MAX):
            return False
    else:
        if not (PRICE_MAX < p <= WATCH_PRICE_MAX):
            return False
    # 価格以外は本来の条件を通ること
    saved = globals()["PRICE_MAX"], globals()["RENT_MAX"]
    try:
        globals()["PRICE_MAX"] = WATCH_PRICE_MAX
        globals()["RENT_MAX"] = WATCH_RENT_MAX
        return apply_rent_filters(item) if t == "rent" else apply_filters(item)
    finally:
        globals()["PRICE_MAX"], globals()["RENT_MAX"] = saved


def track_watchlist(all_raw, prev_watch):
    """監視リストを更新し、値下がりを検知する。
    戻り値: (今回の監視表, 予算内に入ってきた物件, 値下がりした物件)
    """
    now, entered, dropped = {}, [], []
    seen = set()
    for it in all_raw:
        key = it.get("id")
        if not key or key in seen:
            continue
        p = it.get("price")
        if p is None:
            continue
        old = prev_watch.get(key)
        if is_watch_candidate(it):
            seen.add(key)
            now[key] = {"price": p, "name": (it.get("name") or "")[:40],
                        "station": it.get("station"), "type": it.get("type"),
                        "url": it.get("url"), "area": it.get("area"),
                        "first": (old or {}).get("first", p)}
            if old and p < old["price"]:
                it["_watch_note"] = (f"監視中の値下げ {fmt_watch_price(it, old['price'])}"
                                     f"→{fmt_watch_price(it, p)}")
                dropped.append((it, old["price"]))
        elif old:
            # 監視していた物件が予算内に入ってきた
            limit = RENT_MAX if it.get("type") == "rent" else PRICE_MAX
            if p <= limit and old["price"] > limit:
                it["_entered"] = True
                it["_watch_note"] = (f"🎯予算内に下落 {fmt_watch_price(it, old['first'])}"
                                     f"→{fmt_watch_price(it, p)}")
                entered.append((it, old["first"]))
    return now, entered, dropped


def fmt_watch_price(item, p):
    if item.get("type") == "rent":
        return f"{p}万円/月"
    return fmt_price_man(p)


def update_history(items, prev_hist, today):
    """物件ごとの履歴（初回掲載日・初回価格・値下げ記録）を更新する。
    売れ残り日数と値下げ回数は、指値が通るかの判断材料になる。
    戻り値: 今回の履歴
    """
    hist = dict(prev_hist)
    for it in items:
        key = it.get("id")
        p = it.get("price")
        if not key or p is None:
            continue
        h = hist.get(key)
        if h is None:
            h = {"first": today, "first_price": p, "cuts": []}
        else:
            h = dict(h)
            last = h.get("last_price", h.get("first_price"))
            if last is not None and p < last:
                h["cuts"] = (h.get("cuts") or []) + [[today, last, p]]
        h["last_price"] = p
        hist[key] = h

        # 表示用の注記を組み立てる
        days = days_between(h["first"], today)
        notes = []
        if days >= 1:
            notes.append(f"掲載{days}日")
        cuts = h.get("cuts") or []
        if cuts:
            total = h["first_price"] - p
            notes.append(f"値下げ{len(cuts)}回 累計{fmt_watch_price(it, total)}安")
        if notes:
            it["_hist_note"] = " / ".join(notes)
        it["_days"] = days
        it["_cuts"] = len(cuts)
    return hist


def days_between(a, b):
    from datetime import date
    try:
        ya, ma, da = (int(x) for x in a.split("-"))
        yb, mb, db = (int(x) for x in b.split("-"))
        return (date(yb, mb, db) - date(ya, ma, da)).days
    except Exception:
        return 0


def mark_price_changes(items, prev_prices):
    """前回の価格と比べて値下げ/値上げを記録する。
    買い手にとって値下げは強いシグナルなので、通知とページで明示する。
    戻り値: 今回の価格表（次回の比較用）
    """
    now = {}
    for it in items:
        p = it.get("price")
        if p is None:
            continue
        key = it["id"]
        now[key] = p
        old = prev_prices.get(key)
        if old is None or old == p:
            continue
        if it.get("type") == "rent":
            diff = f"{abs(old - p):.1f}万円"
            old_s, new_s = f"{old}万円/月", f"{p}万円/月"
        else:
            diff = fmt_price_man(abs(old - p))
            old_s, new_s = fmt_price_man(old), fmt_price_man(p)
        if p < old:
            it["_price_note"] = f"🔻値下げ {old_s}→{new_s} ({diff}安)"
            it["_price_down"] = True
        else:
            it["_price_note"] = f"🔺値上げ {old_s}→{new_s}"
    n = sum(1 for i in items if i.get("_price_down"))
    if n:
        print(f"値下げを検知: {n}件")
    return now


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": [], "last_run": None, "prices": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def fmt_price_man(p):
    if p is None:
        return ""
    if p >= 10000:
        oku = p // 10000
        rest = p % 10000
        return f"{oku}億{rest:,}万円" if rest else f"{oku}億円"
    return f"{p:,}万円"


# === 通知 ===

def ensure_station_coverage(selected, pool, already):
    """通知に1件も入っていない駅があれば、その駅の最良物件を1件ねじ込む。
    枠は最も件数の多い駅から削る。"""
    have = {i["station"] for i in selected} | {i["station"] for i in already}
    missing = [st for st in STATIONS if st not in have]
    if not missing:
        return selected
    by_station = {}
    for it in pool:
        by_station.setdefault(it["station"], []).append(it)
    added = []
    for st in missing:
        cand = by_station.get(st)
        if not cand:
            continue          # そもそもその駅に候補が無い日は諦める
        added.append(cand[0])
    if not added:
        return selected
    # 多数派の駅から枠を返してもらう
    counts = Counter(i["station"] for i in selected)
    trimmed = list(selected)
    for _ in added:
        if not trimmed:
            break
        top = counts.most_common(1)[0][0]
        for i in range(len(trimmed) - 1, -1, -1):
            if trimmed[i]["station"] == top:
                counts[top] -= 1
                trimmed.pop(i)
                break
    return trimmed + added


def revalidate_walk(item):
    """詳細ページから全駅の徒歩(walks)が取れた物件を再判定する。
    一覧の徒歩は当てにならず、対象駅が最寄りに入っていないことすらある
    （例: 日本橋の検索結果に、最寄りが水天宮前/茅場町の物件が混ざる）。
    True=残す / False=除外。
    """
    walks = item.get("walks")
    if not walks:
        return True                      # 検証材料が無い場合は一覧の値を信じる
    st = item.get("station")
    d = dict(walks)
    if st not in d:
        return False                     # 対象駅が最寄りに存在しない
    limit = WALK_MAX_BY_STATION.get(st,
                                    RENT_WALK_MAX if item.get("type") == "rent" else WALK_MAX)
    if d[st] > limit:
        return False
    item["walk"] = d[st]                 # 実際の値に直す
    return True


def audit_items(items, label=""):
    """送信・公開の直前に、全件が全条件を満たしているか数える。
    「条件を実装したか」ではなく「出てきた物件が条件を満たすか」を見る。
    欠損で判定をスキップしない（過去に住所欠損/築年欠損で条件が素通りした）。
    戻り値: (違反リスト, 集計)
    """
    bad = []

    def ng(it, why):
        bad.append({"why": why, "station": it.get("station"), "type": it.get("type"),
                    "name": (it.get("name") or "")[:28], "url": it.get("url")})

    for it in items:
        t = it.get("type")
        st = it.get("station")

        # 駅徒歩（詳細の walks があればそれを正とする）
        limit = WALK_MAX_BY_STATION.get(st, RENT_WALK_MAX if t == "rent" else WALK_MAX)
        walks = dict(it.get("walks") or [])
        if walks:
            if st not in walks:
                ng(it, f"{st}が最寄り駅に無い({'/'.join(walks)})")
            elif walks[st] > limit:
                ng(it, f"徒歩{walks[st]}分 > 上限{limit}分")
        elif it.get("walk") is None:
            ng(it, "徒歩が不明")
        elif it["walk"] > limit:
            ng(it, f"徒歩{it['walk']}分 > 上限{limit}分")

        # 価格
        p = it.get("price")
        if p is None:
            ng(it, "価格が不明")
        elif t == "rent":
            if not (RENT_MIN <= p <= RENT_MAX):
                ng(it, f"賃料{p}万円 が {RENT_MIN}〜{RENT_MAX}万円 の外")
        elif not (PRICE_MIN <= p <= PRICE_MAX):
            ng(it, f"価格{p}万円 が {PRICE_MIN}〜{PRICE_MAX}万円 の外")

        # 面積
        a = it.get("area")
        amin = RENT_AREA_MIN if t == "rent" else AREA_MIN
        if a is None:
            ng(it, "面積が不明")
        elif a < amin:
            ng(it, f"{a}㎡ < 下限{amin}㎡")

        # 築年（土地は対象外）
        if t in ("mansion", "house", "rent"):
            b = it.get("built")
            amax = RENT_MAX_AGE if t == "rent" else (
                HOUSE_MAX_AGE if t == "house" else MANSION_MAX_AGE)
            if not b:
                ng(it, "築年が不明")
            elif (CURRENT_YEAR - b) >= amax:
                ng(it, f"築{CURRENT_YEAR - b}年 >= 上限{amax}年")

        # 間取り（売買のみ。1LDK/1DK/1Kは対象外）
        if t in ("mansion", "house"):
            lay = it.get("layout") or ""
            if re.match(r"^1[LDKR]+$", lay):
                ng(it, f"間取り{lay} は対象外")

        # エリア
        addr = it.get("addr") or ""
        if not addr:
            ng(it, "住所が不明")
        else:
            allowed = STATION_AREAS.get(st)
            if allowed and not any(x in addr for x in allowed):
                ng(it, f"{addr[:16]} が {'/'.join(allowed)} の外")
            if st == "大井町":
                for pat in OIMACHI_REJECT_PATTERNS:
                    if pat in addr:
                        ng(it, f"大井町の除外エリア({pat})")
                        break

    if bad:
        print(f"\n🚨 条件違反 {len(bad)}件 {label}", file=sys.stderr)
        for b in bad[:20]:
            print(f"   [{b['station']}|{b['type']}] {b['why']} / {b['name']} {b['url']}",
                  file=sys.stderr)
    else:
        print(f"✅ 全{len(items)}件が全条件を満たしています {label}")
    return bad


def rooms_of(item):
    """間取りから部屋数を取る（2LDK→2、1SLDK→1.5、記載なし→0）"""
    lay = item.get("layout") or ""
    m = re.match(r"^(\d+)", lay)
    if not m:
        return 0
    n = float(m.group(1))
    if "S" in lay.upper().replace("SLDK", "S"):
        n += 0.5
    return n


def sort_for_notify(items):
    """優先駅を先頭に、駅ごとにまとめる。駅内は駐車場あり→駅近の順。"""
    order = {s: i for i, s in enumerate(PRIORITY_STATIONS)}
    return sorted(items, key=lambda it: (
        0 if it.get("_price_down") else 1,   # 値下げは最優先で見せる
        order.get(it.get("station"), 99),
        0 if rooms_of(it) >= 2 else 1,       # 2LDK以上を先に見せる
        it.get("station") or "",
        0 if it.get("parking") in ("有", "近隣") else 1,
        it.get("walk") or 99,
    ))


_ALL_ITEMS = []


def notify(new_items):
    print(f"\n=== notify() called with {len(new_items)} items ===")
    print(f"NTFY_TOPIC set: {bool(NTFY_TOPIC)} (len={len(NTFY_TOPIC)})")
    if not new_items:
        print("→ no new items, skipping")
        return
    if not NTFY_TOPIC:
        print("⚠ NTFY_TOPIC未設定、通知スキップ")
        return

    by_station = Counter(it["station"] for it in new_items)
    summary = " / ".join(f"{s}{n}件" for s, n in by_station.most_common())

    # 全件本文に含める。長くなりすぎる場合は複数通知に分割
    TYPE_ICON = {"mansion": "🏢", "house": "🏠", "land": "🏞", "rent": "🔑"}

    def build_lines(items):
        lines = []
        cur_station = None
        for it in items:
            # 駅が変わったら見出しを入れる（エリアの区切りを分かりやすく）
            if it.get("station") != cur_station:
                cur_station = it.get("station")
                mark = "★" if cur_station in PRIORITY_STATIONS else ""
                lines.append(f"───── {mark}{cur_station} ─────")
            icon = TYPE_ICON.get(it.get("type"), "・")
            head = f"{icon}[{it['station']}] {it['name'][:24]}"

            def fmt(v, suffix=""):
                if v is None or v == "" or v == 0:
                    return "記載なし"
                return f"{v}{suffix}"

            if it.get("type") == "rent":
                # 賃貸: 管理費込み表示
                price = f"{it['price']}万円/月(管理費込)"
            else:
                price = fmt_price_man(it.get('price')) or "記載なし"
            layout = "" if it.get("type") == "land" else fmt(it.get("layout"))
            area = fmt(it.get('area'), "㎡")
            walks = it.get("walks")
            if walks:
                walk = " / ".join(f"{n} 徒歩{m}分" for n, m in walks[:5])
            elif it.get("walk"):
                walk = f"{it['station']} 徒歩{it['walk']}分"
            else:
                walk = "徒歩記載なし"
            built = it.get('built')
            if built:
                yrs = CURRENT_YEAR - built
                age_str = "新築" if yrs <= 0 else f"築{yrs}年"
            else:
                age_str = "築年記載なし" if it["type"] in ("mansion", "house", "rent") else ""
            # 所在階は全物件に書く。取れなければ明記して伏せない
            if it["type"] == "land":
                floor_str = "階なし(土地)"
            else:
                floor_str = it.get("floor") or "階記載なし"
            pk = it.get("parking")
            pk_str = {
                "有":   "🚗駐車場あり",
                "近隣": "🚗駐車場あり(近隣)",
                "空無": "🚗駐車場あり(空きなし)",
                "無":   "駐車場なし",
            }.get(pk, "駐車場記載なし")
            pp = it.get("parking_price")
            if pp and pk in ("有", "近隣", "空無"):
                pk_str += f" {pp}"
            dup_str = it.get("_dup_note", "")
            price_note = it.get("_price_note", "") or it.get("_watch_note", "")
            hist_note = it.get("_hist_note", "")
            sr = it.get("shikirei", "") if it.get("type") == "rent" else ""
            parts = [price, layout, area, floor_str, walk, age_str, sr, pk_str,
                     price_note, hist_note, dup_str]
            meta = " ".join(p for p in parts if p)
            lines.append(f"{head}\n  {meta}\n  {it['url']}")
        return lines

    POOL_URL = "https://imukte555.github.io/bukken-pool/"

    # メールは在庫“全件”。プッシュは1通に要約（全件を通知で流すと十数通になる）
    mail_items = sort_for_notify(_ALL_ITEMS) if _ALL_ITEMS else new_items
    new_ids = {i["id"] for i in new_items}
    by_type = Counter(it.get("type") for it in mail_items)
    tsum = " ".join(f"{TYPE_ICON.get(k,'')}{v}" for k, v in by_type.most_common())
    n_new = sum(1 for i in mail_items if i["id"] in new_ids and not i.get("_filler"))
    title = f"🏠 在庫{len(mail_items)}件 (新着{n_new}) {tsum}"

    # プッシュ通知は1通だけ。駅ごとの件数と新着数、そしてページへのリンク。
    st_line = " / ".join(f"{s}{n}" for s, n in
                         Counter(i["station"] for i in mail_items).most_common(8))
    chunks = [f"新着{n_new}件 / 在庫{len(mail_items)}件\n{st_line}\n\n▼全件を見る\n{POOL_URL}"]
    print(f"  body chunks: {len(chunks)} (プッシュは要約1通)")
    click_url = new_items[0]["url"]

    STATION_ROMAJI = {
        "大井町": "Oimachi", "恵比寿": "Ebisu", "広尾": "Hiroo",
        "代官山": "Daikanyama", "目黒": "Meguro", "中目黒": "Nakameguro",
        "五反田": "Gotanda", "武蔵小山": "MusashiKoyama", "不動前": "Fudomae",
        "戸越": "Togoshi", "蒲田": "Kamata", "京急蒲田": "KeikyuKamata",
    }
    actions_list = []
    for i, it in enumerate(new_items[:3]):
        label = f"#{i+1} {STATION_ROMAJI.get(it['station'], 'Open')}"
        actions_list.append(f"view, {label}, {it['url']}, clear=true")
    actions_header = "; ".join(actions_list)

    title_encoded = "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode("ascii") + "?="

    headers = {
        "Title": title_encoded,
        "Click": click_url,
        "Priority": "max",  # 睡眠/集中モードでも音・バナーを出す
        "Tags": "house",
        "Markdown": "no",
        "Actions": actions_header,
    }
    # 注: ntfy.sh無料版は匿名メール転送不可(Emailヘッダーは400になる)ため使わない。
    # メールが必要なら GitHub Actions 側で SMTP ステップを足す。
    print(f"→ POSTing to ntfy.sh/{NTFY_TOPIC[:3]}*** (priority=max)")
    print(f"  title: {title[:80]}")
    print(f"  click: {click_url[:80]}")
    print(f"  actions: {actions_header[:150]}")
    for i, chunk in enumerate(chunks):
        chunk_title = title if i == 0 else f"({i+1}/{len(chunks)}) 続き"
        title_enc = "=?UTF-8?B?" + base64.b64encode(chunk_title.encode("utf-8")).decode("ascii") + "?="
        h2 = dict(headers)
        h2["Title"] = title_enc
        if i > 0:
            h2.pop("Actions", None)
            h2.pop("Click", None)
        # リトライ付き送信（タイムアウトでチャンクが欠落するのを防ぐ）
        sent = False
        for attempt in range(3):
            try:
                r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                                  data=chunk.encode("utf-8"), headers=h2, timeout=30)
                print(f"  ntfy POST chunk {i+1}/{len(chunks)}: HTTP {r.status_code} (body {len(chunk)}B)")
                if r.status_code == 200:
                    sent = True
                    break
                print(f"    response: {r.text[:200]}", file=sys.stderr)
            except Exception as e:
                print(f"  ntfy chunk {i+1} 試行{attempt+1} 失敗: {e}", file=sys.stderr)
            time.sleep(4 * (attempt + 1))
        if not sent:
            print(f"  ⚠ chunk {i+1}/{len(chunks)} 送信失敗（メールで届く想定）", file=sys.stderr)
        time.sleep(1.5)   # ntfy側のレート制限回避

    # メール送信（取りこぼしゼロの主経路）
    mail_body = ("▼全件を見る\n" + POOL_URL + "\n\n"
                 + "\n\n".join(build_lines(mail_items))
                 + "\n\n▼全件を見る\n" + POOL_URL)
    send_email(title, mail_body)


def send_email(subject, body):
    """Gmail SMTP で確実にメール送信。GMAIL_USER / GMAIL_APP_PASSWORD が必要。"""
    user = os.environ.get("GMAIL_USER", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    to = os.environ.get("MAIL_TO", user).strip() or user
    if not user or not pw:
        print("  メール: GMAIL_USER/GMAIL_APP_PASSWORD未設定、スキップ")
        return
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        print(f"  メール送信成功 → {to}")
    except Exception as e:
        print(f"  メール送信失敗: {e}", file=sys.stderr)


def main():
    print(f"=== Property Watcher start ===")
    items = collect_all()

    # 重複ID除外
    dedup = {}
    for it in items:
        dedup.setdefault(it["id"], it)
    items = list(dedup.values())

    # 同一物件の重複除外（別ポータル/別掲載で同じ物件が並ぶのを防ぐ）
    # 判定キー: 駅 + 種別 + 価格 + 面積（小数1桁）
    seen_prop = set()
    uniq_items = []
    # 情報量の多いソース(実名がある)を優先して残す
    for it in sorted(items, key=lambda x: (0 if x.get("source") == "SUUMO" else 1)):
        a = it.get("area")
        # 住所を入れないと「同じ駅・同じ価格・同じ面積の別の土地」を
        # 同一物件として消してしまう（土地は間取りが空なので特に起きる）
        key = (it.get("station"), it.get("type"), it.get("price"),
               round(a, 1) if a else None, it.get("layout"),
               (it.get("addr") or "")[:14])
        if key in seen_prop:
            continue
        seen_prop.add(key)
        uniq_items.append(it)
    if len(uniq_items) < len(items):
        print(f"同一物件の重複を除外: {len(items)} → {len(uniq_items)}件")
    items = uniq_items

    global _ALL_ITEMS

    # プール全件を1枚のHTMLに出す（毎朝の通知とは別。今ある在庫を全部見るため）
    # 通知30件だけだと、在庫を見終わる前に消える物件が出る＝機会損失になる。
    try:
        print(f"在庫ページ用に全件の詳細を取得中… ({len(items)}件)")
        # 取得元が増えて500件超になり、5並列だと詳細取得だけで長時間かかる。
        # ホストごとのゲート(_HOST_GATE)で同時接続は制御されるので、
        # プール全体の並列数は上げてよい。
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
            list(ex.map(enrich_from_detail, items))
        before = len(items)
        items = [i for i in items if revalidate_walk(i)]
        if len(items) < before:
            print(f"徒歩の再判定で除外: {before} → {len(items)}件")
        # 再判定“後”のリストをプールとして使う。
        # 前に代入すると、徒歩超過で弾いた物件がメールに残ってしまう。
        # 全件を全条件と突き合わせる。違反は載せない（見つけ次第ログに出す）
        violations = audit_items(items, "(在庫ページ/メール)")
        if violations:
            bad_urls = {v["url"] for v in violations}
            items = [i for i in items if i.get("url") not in bad_urls]
            print(f"条件違反を除外: {len(items)}件に", file=sys.stderr)
        # 値下げ・掲載日数・監視リストはページ生成より前に計算する。
        # 後回しにすると注記(_hist_note等)が間に合わずページに出ない。
        _state = load_state()
        price_now = mark_price_changes(items, _state.get("prices", {}) or {})
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _today = _dt.now(_tz(_td(hours=9))).strftime("%Y-%m-%d")
        history_now = update_history(items, _state.get("history", {}) or {}, _today)
        _long = [i for i in items if (i.get("_days") or 0) >= 90]
        _cut = [i for i in items if (i.get("_cuts") or 0) >= 1]
        print(f"履歴: 90日以上の売れ残り{len(_long)}件 / 値下げ実績あり{len(_cut)}件")
        watch_now, entered, dropped = track_watchlist(
            _RAW_FOR_WHATIF, _state.get("watchlist", {}) or {})
        print(f"監視リスト: {len(watch_now)}件（予算オーバーだが条件は満たす）")
        if entered:
            print(f"🎯 予算内に下がってきた物件: {len(entered)}件")
            for _it, _first in entered:
                print(f"   [{_it['station']}] {_it.get('name','')[:30]} "
                      f"{fmt_watch_price(_it, _first)}→{fmt_watch_price(_it, _it['price'])}")

        _ALL_ITEMS = items
        # 取得が途中で潰れた回に、少ない件数でページを上書きしてプールを
        # 消してしまわないための歯止め。前回の6割を割ったら書き換えない。
        # （実測: アットホームのIPブロックで取得が3時間止まった回がある）
        prev_n = int(_state.get("page_count", 0) or 0)
        globals()["_PAGE_COUNT"] = prev_n   # 生成できなければ前回値を維持
        if prev_n and len(items) < prev_n * 0.6:
            print(f"⚠️ 取得件数が前回より大きく減ったため在庫ページを更新しません "
                  f"(前回{prev_n}件 → 今回{len(items)}件)。"
                  f"打ち切ったサイト: {'、'.join(sorted(_HOST_DEAD)) or 'なし'}",
                  file=sys.stderr)
            raise RuntimeError(f"件数が前回の6割未満 ({len(items)}/{prev_n})")
        import gen_page
        # 駅タブは左から優先順（恵比寿/目黒/中目黒 → 以降はSTATIONS定義順）
        order = PRIORITY_STATIONS + [s for s in STATIONS if s not in PRIORITY_STATIONS]
        n = gen_page.build(items, str(BASE_DIR / "docs" / "index.html"),
                           station_order=order, reject_tally=REJECT_TALLY)
        globals()["_PAGE_COUNT"] = n
        print(f"在庫ページ生成: {n}件 → docs/index.html")
    except Exception as e:
        print(f"在庫ページ生成に失敗: {e}", file=sys.stderr)
        _ALL_ITEMS = items
        _st2 = load_state()
        price_now = mark_price_changes(items, _st2.get("prices", {}) or {})
        from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
        _today = _dt2.now(_tz2(_td2(hours=9))).strftime("%Y-%m-%d")
        history_now = update_history(items, _st2.get("history", {}) or {}, _today)
        watch_now, entered, dropped = track_watchlist(
            _RAW_FOR_WHATIF, _st2.get("watchlist", {}) or {})
    print_reject_tally()
    try:
        whatif_report(_RAW_FOR_WHATIF, len(items))
    except Exception as e:
        print(f"試算に失敗: {e}", file=sys.stderr)
    print(f"\n総取得(dedupe後): {len(items)}件")

    # 欠損フィールドを詳細ページから補完（できる範囲で）
    def is_incomplete(it):
        if not it.get("price") or not it.get("area") or not it.get("walk"):
            return True
        if it["type"] in ("mansion", "house"):
            if not it.get("layout") or not it.get("built"):
                return True
        return False

    incomplete = [it for it in items if is_incomplete(it)]
    if incomplete:
        print(f"\n欠損補完 {len(incomplete)}件: 詳細ページfetch")
        for it in incomplete:
            fill_missing_from_detail(it)
            time.sleep(0.6)
    # 取れなかったフィールドは通知本文で「記載なし」と表示する（除外しない）

    state = load_state()
    seen = set(state.get("seen_ids", []))
    if dropped:
        print(f"監視中の値下げ: {len(dropped)}件")

    if not seen:
        print("初回実行: スナップショットのみ保存（通知なし）")
        save_state({"seen_ids": [it["id"] for it in items], "last_run": int(time.time()),
                    "prices": price_now, "watchlist": watch_now,
                    "history": history_now, "page_count": _PAGE_COUNT})
        return

    new_items = [it for it in items if it["id"] not in seen]
    print(f"純粋な新着: {len(new_items)}件")

    # --- 毎日TARGET_MIN_ITEMS件以上に増量 ---
    # 新着が目標に満たない場合、既出の中から「オススメ枠」で補充する。
    # 補充の優先度: 駐車場あり > 単価/賃料が安い > 駅に近い
    if len(new_items) < TARGET_MIN_ITEMS:
        need = TARGET_MIN_ITEMS - len(new_items)
        new_ids = {it["id"] for it in new_items}
        pool = [it for it in items if it["id"] not in new_ids]

        def score(it):
            pk = 0 if it.get("parking") in ("有", "近隣") else 1   # 駐車場ありを先に
            w = it.get("walk") or 99
            # 賃貸は賃料、売買は㎡単価で安い順
            if it.get("type") == "rent":
                unit = it.get("price") or 999
            else:
                a = it.get("area") or 1
                unit = (it.get("price") or 99999) / a
            return (pk, w, unit)

        # 補充でも優先駅（恵比寿/目黒/中目黒）を先に埋める
        pool.sort(key=score)
        prio_pool = [i for i in pool if i.get("station") in PRIORITY_STATIONS]
        rest_pool = [i for i in pool if i.get("station") not in PRIORITY_STATIONS]
        take_prio = min(len(prio_pool), max(0, min(need, PRIORITY_RESERVED)))
        filler = prio_pool[:take_prio] + rest_pool[:need - take_prio]

        # どの駅も最低1件は入れる（駅が丸ごと欠けるのを防ぐ）
        filler = ensure_station_coverage(filler, pool, new_items)
        filler = filler[:need]
        print(f"補充{need}件のうち優先駅から{take_prio}件 "
              f"(駅カバー: {len({i['station'] for i in filler})}駅)")
        for it in filler:
            it["_filler"] = True
        new_items = new_items + filler
        print(f"→ 目標{TARGET_MIN_ITEMS}件に補充: +{len(filler)}件 (計{len(new_items)}件)")

    # --- 上限カット: 種別バランスを取りつつ良い順に TARGET_MAX_ITEMS 件へ ---
    if True:
        def quality(it):
            pk = 0 if it.get("parking") in ("有", "近隣") else 1
            w = it.get("walk") or 99
            if it.get("type") == "rent":
                unit = it.get("price") or 999          # 賃料が安い順
            else:
                a = it.get("area") or 1
                unit = (it.get("price") or 99999) / a  # ㎡単価が安い順
            return (pk, w, unit)

        # 種別ごとの枠（合計30）: 土地9 / 戸建8 / 賃貸7 / マンション6
        quota = {"land": 12, "house": 7, "rent": 6, "mansion": 5}

        # 種別ごとに「優先駅 → その他の新着 → プール(既出)」の順で埋める。
        # 優先駅を先に一括確保すると種別が偏る（目黒/中目黒は賃貸が多い）ので
        # 種別の中で優先駅を先に取る形にする。
        pool_rest = [i for i in _ALL_ITEMS]
        picked, picked_ids = [], set()

        def take(cands, n):
            got = []
            for it in cands:
                if n <= 0:
                    break
                if it["id"] in picked_ids:
                    continue
                got.append(it)
                picked_ids.add(it["id"])
                n -= 1
            return got

        for t, n in quota.items():
            new_t = sorted([i for i in new_items if i.get("type") == t], key=quality)
            prio_t = [i for i in new_t if i.get("station") in PRIORITY_STATIONS]
            other_t = [i for i in new_t if i.get("station") not in PRIORITY_STATIONS]
            sel = take(prio_t, min(n, PRIORITY_PER_TYPE))
            sel += take(other_t, n - len(sel))
            if len(sel) < n:
                # 新着で足りないぶんはプールから補う。賃貸で埋め合わせない。
                pool_t = sorted([i for i in pool_rest if i.get("type") == t], key=quality)
                pool_prio = [i for i in pool_t if i.get("station") in PRIORITY_STATIONS]
                add = take(pool_prio, min(n - len(sel), PRIORITY_PER_TYPE))
                add += take(pool_t, n - len(sel) - len(add))
                for c in add:
                    c["_filler"] = True
                sel += add
            if len(sel) < n:
                print(f"  {t}は{len(sel)}件しか確保できず(枠{n})")
            picked.extend(sel)

        if len(picked) < TARGET_MAX_ITEMS:
            rest = sorted([i for i in _ALL_ITEMS if i["id"] not in picked_ids], key=quality)
            for c in take(rest, TARGET_MAX_ITEMS - len(picked)):
                c["_filler"] = True
                picked.append(c)
        picked = picked[:TARGET_MAX_ITEMS]
        bt = Counter(i.get("type") for i in picked)
        ps = Counter(i.get("station") for i in picked if i.get("station") in PRIORITY_STATIONS)
        print(f"  種別実績: 土地{bt['land']}/戸建{bt['house']}/賃貸{bt['rent']}/マンション{bt['mansion']}")
        print(f"  優先駅: {dict(ps)}")
        new_items = picked
        print(f"→ 上限{TARGET_MAX_ITEMS}件にカット (種別バランス: 土地12/戸建7/賃貸6/マンション5)")

    # --- 駐車場チェック（確定した通知対象のみ詳細fetch） ---
    # 詳細ページを1回だけ取得して、駐車場と全駅からの徒歩を埋める
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(enrich_from_detail, new_items))


    # --- 最終並び順: 駐車場あり → 新着 → 駅近 ---
    # エリアがバラバラだと読みにくいので駅ごとにまとめる。優先駅を先頭に。
    station_order = {s: i for i, s in enumerate(PRIORITY_STATIONS)}
    new_items.sort(key=lambda it: (
        station_order.get(it.get("station"), 99),
        it.get("station") or "",
        0 if it.get("parking") in ("有", "近隣") else 1,
        it.get("walk") or 99,
    ))

    print(f"通知対象: {len(new_items)}件")
    for it in new_items[:45]:
        tag = "[既出]" if it.get("_filler") else "[新着]"
        pk = "🚗" if it.get("parking") in ("有", "近隣") else "  "
        print(f"  {tag}{pk} [{it['station']}|{it['source']}] {it['name'][:22]} {it.get('price')} {it.get('layout')} {it.get('area')}㎡ 徒歩{it.get('walk')}分")

    # DEBUG=1 環境変数で強制通知（テスト用）
    if os.environ.get("DEBUG_FORCE_NOTIFY") == "1":
        print(f"DEBUG_FORCE_NOTIFY=1: 全{len(items)}件を仮新着として通知")
        new_items = items

    # 予算内に下がってきた物件は最優先で通知に入れる
    if entered:
        ent_items = [it for it, _ in entered]
        ent_ids = {i["id"] for i in ent_items}
        new_items = ent_items + [i for i in new_items if i["id"] not in ent_ids]

    # 同じ日に2回通知しない（予備の実行が走っても二重送信しないため）
    from datetime import datetime, timezone, timedelta
    today_jst = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    prev = load_state()
    already = prev.get("notified_on") == today_jst
    forced = os.environ.get("DEBUG_FORCE_NOTIFY") == "1"
    # 手動実行(workflow_dispatch)は動作確認用。定刻前なら通知枠を消費しない。
    # 消費すると、その日の自動実行がスキップされて朝のメールが届かなくなる。
    # ただし定刻を過ぎてまだ未送信なら、手動実行でもその日の1通を送る。
    # GitHubは同じconcurrencyグループで待機中の古いrunを破棄するため、
    # 手動実行が定期実行を潰してメールが1日欠ける事故が実際に起きた
    # (2026-09-09 06:37 JSTの定期実行がcancelled)。その穴を埋める。
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    now_jst = datetime.now(timezone(timedelta(hours=9)))
    after_sched = (now_jst.hour, now_jst.minute) >= SCHEDULE_JST
    manual_hold = manual and not after_sched
    if already and not forced:
        print(f"本日({today_jst})は通知済みのため送信をスキップ（ページのみ更新）")
    elif manual_hold and not forced:
        print(f"手動実行かつ定刻({SCHEDULE_JST[0]:02d}:{SCHEDULE_JST[1]:02d} JST)前のため"
              "通知はスキップ（自動実行の枠を残す。ページのみ更新）")
    else:
        notify(new_items)
        already = False   # 送ったので notified_on を今日に更新する

    # ハートビート: 毎日1通必ず送る (新着0でも、動作確認用)
    if not new_items:
        by_station_all = Counter(it["station"] for it in items)
        summary = " / ".join(f"{s}{n}" for s, n in by_station_all.most_common())
        send_heartbeat(len(items), summary)

    save_state({"seen_ids": [it["id"] for it in items], "last_run": int(time.time()),
                "prices": price_now,
                "watchlist": watch_now,
                "history": history_now,
                "notified_on": prev.get("notified_on")
                               if (already or (manual_hold and not forced))
                               else today_jst,
                "page_count": _PAGE_COUNT})


def send_heartbeat(total, summary):
    if not NTFY_TOPIC:
        return
    title = "💓 ウォッチャー稼働中 (新着0)"
    body = f"今日の取得: {total}件\n内訳: {summary}\n\n新着がなくても動いてます。"
    title_enc = "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?="
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": title_enc, "Priority": "low", "Tags": "heart"},
            timeout=15,
        )
        print(f"heartbeat: HTTP {r.status_code}")
    except Exception as e:
        print(f"heartbeat error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
