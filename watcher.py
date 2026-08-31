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
MAX_ROOMS_PER_BUILDING = 3  # 同一建物から通知する最大部屋数

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
PRICE_MAX = 12000    # 1.2億（建物込みの予算上限）
BUILT_MAX_AGE = 30
CURRENT_YEAR = 2026

# === フィルタ（賃貸） ===
RENT_MAX = 28.0      # 管理費込み上限(万円)
RENT_MIN = 8.0       # 下限(万円) 安すぎる1Rを除外
RENT_AREA_MIN = 40.0 # 賃貸の面積下限(㎡)
RENT_WALK_MAX = 7    # 賃貸の駅徒歩上限(分) ※売買と同じ7分
RENT_MAX_AGE = 20    # 賃貸の築年数上限(年) ※20年未満のみ
HOUSE_MAX_AGE = 20   # 戸建の築年数上限(年) ※20年未満のみ
MANSION_MAX_AGE = 20 # 中古マンションの築年数上限(年) ※20年未満のみ

# 1日に通知する件数（目安30件。少ない日は補充、多い日は上限でカット）
TARGET_MIN_ITEMS = 30
TARGET_MAX_ITEMS = 30

# === 駅コード ===
STATIONS = {
    "大井町": {"suumo": "05480", "homes": "oimachi_00603-st",    "nomu": "ensen_tokyo/2196/2196270", "livable": "tokyo/s2196270", "athome": "oimachi-st"},
    "恵比寿": {"suumo": "05050", "homes": "ebisu_00577-st",       "nomu": "ensen_tokyo/2172/2172100", "livable": "tokyo/s2172100", "athome": "ebisu-st"},
    "広尾":   {"suumo": "33410", "homes": "hiro_06347-st",        "nomu": "ensen_tokyo/2344/2344190", "livable": "tokyo/s2344190", "athome": "hiro-st"},
    "代官山": {"suumo": "21850", "homes": "daikanyama_05050-st",  "nomu": "ensen_tokyo/2321/2321020", "livable": "tokyo/s2321020", "athome": "daikanyama-st"},
    "目黒":   {"suumo": "39110", "homes": "meguro_00577-st",      "nomu": "ensen_tokyo/2172/2172090", "livable": "tokyo/s2172090", "athome": "meguro-st"},
    "中目黒": {"suumo": "27580", "homes": "nakameguro_00577-st",  "nomu": "ensen_tokyo/2321/2321030", "livable": "tokyo/s2321030", "athome": "nakameguro-st"},
    "五反田": {"suumo": "14970", "homes": "gotanda_00603-st",     "nomu": "ensen_tokyo/2172/2172080", "livable": "tokyo/s2172080", "athome": "gotanda-st"},
    "武蔵小山": {"suumo": "38730", "homes": "musashikoyama_05069-st", "nomu": "ensen_tokyo/2327/2327230", "livable": "tokyo/s2327230", "athome": "musashikoyama-st"},
    "不動前": {"suumo": "34410", "homes": "fudomae_05068-st",     "nomu": "ensen_tokyo/2327/2327220", "livable": "tokyo/s2327220", "athome": "fudomae-st"},
    "戸越":   {"suumo": "26080", "homes": "togoshi_06400-st",     "nomu": "ensen_tokyo/2351/2351170", "livable": "tokyo/s2351170", "athome": "togoshi-st"},
    "蒲田":   {"suumo": "08940", "homes": "kamata_00605-st",      "nomu": "ensen_tokyo/2196/2196290", "livable": "tokyo/s2196290", "athome": "kamata-st"},
    "京急蒲田": {"suumo": "13410", "homes": "keikyukamata_05144-st", "nomu": "ensen_tokyo/2331/2331120", "livable": "tokyo/s2331120", "athome": "keikyukamata-st"},
    "泉岳寺": {"suumo": "21340", "homes": "sengakuji_05181-st",  "nomu": "ensen_tokyo/2351/2351140", "livable": "tokyo/s2351140", "athome": "sengakuji-st"},
    "高輪ゲートウェイ": {"suumo": "84570", "homes": "takanawagateway_10177-st", "nomu": "ensen_tokyo/2172/2172056", "livable": "tokyo/s2172056", "athome": "takanawagateway-st"},
    "三田":   {"suumo": "36860", "homes": "mita_06402-st",       "nomu": "ensen_tokyo/2351/2351130", "livable": "tokyo/s2351130", "athome": "mita-st"},
    "大門":   {"suumo": "22090", "homes": None,                  "nomu": "ensen_tokyo/2351/2351120", "livable": "tokyo/s2351120", "athome": "daimon-st"},
    "新橋":   {"suumo": "20110", "homes": None,                  "nomu": "ensen_tokyo/2351/2351110", "livable": "tokyo/s2351110", "athome": "shimbashi-st"},
    "日本橋": {"suumo": "29710", "homes": None,                  "nomu": "ensen_tokyo/2351/2351080", "livable": "tokyo/s2351080", "athome": None},
    "東日本橋": {"suumo": "32170", "homes": None,                "nomu": "ensen_tokyo/2351/2351060", "livable": "tokyo/s2351060", "athome": None},
    # --- 大井町・蒲田の周辺と目黒線沿い（2026-08-29 追加。コードは実URLで検証済み）---
    "青物横丁": {"suumo": "00240", "homes": None, "nomu": "ensen_tokyo/2331/2331050", "livable": "tokyo/s2331050", "athome": None},
    "新馬場": {"suumo": "20140", "homes": None, "nomu": "ensen_tokyo/2331/2331040", "livable": "tokyo/s2331040", "athome": None},
    "大森":   {"suumo": "06360", "homes": None, "nomu": "ensen_tokyo/2196/2196280", "livable": "tokyo/s2196280", "athome": None},
    "大崎":   {"suumo": "05780", "homes": None, "nomu": "ensen_tokyo/2172/2172070", "livable": "tokyo/s2172070", "athome": None},
    "天王洲アイル": {"suumo": "25440", "homes": None, "nomu": None, "livable": None, "athome": None},
    "品川":   {"suumo": "17460", "homes": None, "nomu": "ensen_tokyo/2196/2196260", "livable": "tokyo/s2196260", "athome": None},
    "西小山": {"suumo": "28780", "homes": None, "nomu": "ensen_tokyo/2327/2327240", "livable": "tokyo/s2327240", "athome": None},
    "洗足":   {"suumo": "21470", "homes": None, "nomu": "ensen_tokyo/2327/2327250", "livable": "tokyo/s2327250", "athome": None},
    # --- 2026-08-31 追加。大井町/戸越/蒲田の周辺と浅草線沿い。
    #     コードはSUUMO・ノムコムとも実URLで1駅ずつ検証済み ---
    "立会川":   {"suumo": "23090", "homes": None, "nomu": "ensen_tokyo/2331/2331070", "livable": "tokyo/s2331070", "athome": None},
    "鮫洲":     {"suumo": "16530", "homes": None, "nomu": "ensen_tokyo/2331/2331060", "livable": "tokyo/s2331060", "athome": None},
    "大森海岸": {"suumo": "06380", "homes": None, "nomu": "ensen_tokyo/2331/2331080", "livable": "tokyo/s2331080", "athome": None},
    "下神明":   {"suumo": "18100", "homes": None, "nomu": "ensen_tokyo/2323/2323020", "livable": "tokyo/s2323020", "athome": None},
    "戸越公園": {"suumo": "26100", "homes": None, "nomu": "ensen_tokyo/2323/2323030", "livable": "tokyo/s2323030", "athome": None},
    "中延":     {"suumo": "27370", "homes": None, "nomu": "ensen_tokyo/2323/2323040", "livable": "tokyo/s2323040", "athome": None},
    "荏原町":   {"suumo": "05030", "homes": None, "nomu": "ensen_tokyo/2323/2323050", "livable": "tokyo/s2323050", "athome": None},
    "旗の台":   {"suumo": "30650", "homes": None, "nomu": "ensen_tokyo/2325/2325050", "livable": "tokyo/s2325050", "athome": None},
    "大岡山":   {"suumo": "05520", "homes": None, "nomu": "ensen_tokyo/2327/2327260", "livable": "tokyo/s2327260", "athome": None},
    "梅屋敷":   {"suumo": "04660", "homes": None, "nomu": "ensen_tokyo/2331/2331110", "livable": "tokyo/s2331110", "athome": None},
    "雑色":     {"suumo": "21660", "homes": None, "nomu": "ensen_tokyo/2331/2331130", "livable": "tokyo/s2331130", "athome": None},
    "大森町":   {"suumo": "06400", "homes": None, "nomu": "ensen_tokyo/2331/2331100", "livable": "tokyo/s2331100", "athome": None},
    "糀谷":     {"suumo": "14030", "homes": None, "nomu": None, "livable": None, "athome": None},
    "人形町":   {"suumo": "29780", "homes": None, "nomu": "ensen_tokyo/2351/2351070", "livable": "tokyo/s2351070", "athome": None},
    "浅草橋":   {"suumo": "00680", "homes": None, "nomu": "ensen_tokyo/2351/2351050", "livable": "tokyo/s2351050", "athome": None},
    "蔵前":     {"suumo": "13010", "homes": None, "nomu": "ensen_tokyo/2351/2351040", "livable": "tokyo/s2351040", "athome": None},
    "東銀座":   {"suumo": "31870", "homes": None, "nomu": "ensen_tokyo/2351/2351100", "livable": "tokyo/s2351100", "athome": None},
    "宝町":     {"suumo": "22650", "homes": None, "nomu": "ensen_tokyo/2351/2351090", "livable": "tokyo/s2351090", "athome": None},
    "浜松町":   {"suumo": "31160", "homes": None, "nomu": "ensen_tokyo/2196/2196240", "livable": "tokyo/s2196240", "athome": None},
    "田町":     {"suumo": "23500", "homes": None, "nomu": "ensen_tokyo/2196/2196250", "livable": "tokyo/s2196250", "athome": None},
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
    "青物横丁": ["品川区"],
    "新馬場":   ["品川区"],
    "大森":     ["大田区", "品川区"],
    "大崎":     ["品川区"],
    "天王洲アイル": ["品川区"],
    "品川":     ["港区", "品川区"],
    "西小山":   ["品川区", "目黒区"],
    "洗足":     ["目黒区", "大田区", "品川区"],
    "立会川":   ["品川区"],
    "鮫洲":     ["品川区"],
    "大森海岸": ["品川区", "大田区"],
    "下神明":   ["品川区"],
    "戸越公園": ["品川区"],
    "中延":     ["品川区"],
    "荏原町":   ["品川区"],
    "旗の台":   ["品川区", "大田区"],
    "大岡山":   ["大田区", "目黒区"],
    "梅屋敷":   ["大田区"],
    "雑色":     ["大田区"],
    "大森町":   ["大田区"],
    "糀谷":     ["大田区"],
    "人形町":   ["中央区"],
    "浅草橋":   ["台東区", "中央区"],
    "蔵前":     ["台東区"],
    "東銀座":   ["中央区"],
    "宝町":     ["中央区"],
    "浜松町":   ["港区"],
    "田町":     ["港区"],
}

OIMACHI_REJECT_PATTERNS = [
    "西大井", "西品川", "東品川", "二葉", "豊町",
    "南大井",
    "大井5丁目", "大井5-", "大井6丁目", "大井6-", "大井7丁目",
    "南品川3丁目", "南品川3-", "南品川4丁目", "南品川4-",
]


# === ユーティリティ ===

def fetch(url: str, impersonate: bool = False) -> str:
    """通常はrequests。impersonate=True でChrome TLS指紋偽装 (Cloudflare突破)"""
    try:
        if impersonate and HAS_CFFI:
            r = cffi_requests.get(url, impersonate="chrome120", timeout=TIMEOUT)
        else:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=TIMEOUT)
        if r.status_code == 200 and r.text:
            return r.text
        print(f"  HTTP {r.status_code} (len={len(r.text)}): {url}", file=sys.stderr)
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


def parse_walk(text: str, station: str = None):
    """駅徒歩(分)を返す。
    station指定時はその駅の徒歩のみ。指定なしの場合は
    「駅名」徒歩X分 のような駅名直後パターンのみ採用。
    PRテキストの「○○まで徒歩X分」は除外。
    """
    # 駅指定: 駅名直後の徒歩X分のみ
    if station:
        s = re.escape(station)
        patterns = [
            rf"「{s}」駅?\s*徒?歩\s*(\d+)\s*分",           # 「大井町」駅 徒歩4分 / 「大井町」徒歩4分
            rf"「{s}駅」\s*徒?歩\s*(\d+)\s*分",            # 「大井町駅」徒歩4分
            rf"{s}駅\s*徒?歩\s*(\d+)\s*分",                # 大井町駅 徒歩4分
            rf"{s}\s*駅?\s*[\s]?徒?歩\s*(\d+)\s*分",       # 大井町 徒歩4分
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


def parse_addr(text: str):
    m = re.search(r"((?:品川区|渋谷区|目黒区|港区)[^\s　<>「」]+)", text)
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
            "source": "アットホーム",
        })
    return items


# === ノムコム パーサー ===

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
    for a in soup.find_all("a", href=lambda h: h and re.search(r"/(?:mansion|kodate)/C\d{6,12}", h or "")):
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
    use_cffi = item.get("source") in ("HOMES", "アットホーム")
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

    if not item.get("walks"):
        transit = _row_value(soup, "交通", "駅徒歩", "最寄") or soup.get_text(" ", strip=True)
        walks = parse_all_walks(transit)
        if walks:
            item["walks"] = walks


# === コレクター ===

# HOMES/アットホームがbot検知でブロックし始めたら、そのホストへのアクセスを
# 一定時間止める。ブロック中に叩き続けるとブロックが延びるだけで無駄。
_BLOCKED_UNTIL = {}
_BLOCK_LOCK = threading.Lock()
BLOCK_COOLDOWN = 300  # 秒

# bot検知が厳しいサイトは「同時1本 + 最低間隔」で叩く。
# 並列化した状態で普通に投げると即ブロックされるため。
_HOST_GATE = {
    "www.homes.co.jp":  (threading.Lock(), 4.0),
    "www.athome.co.jp": (threading.Lock(), 4.0),
    # SUUMOは並列で叩くと503を連発する（実測157件）。同時1本+1.2秒間隔。
    "suumo.jp":         (threading.Lock(), 1.3),
}
_HOST_LAST = {}


# 503が出たら間隔を自動で広げる。ブロックの原因は通信方法ではなく
# 短時間のアクセス回数なので、詰まったら黙って遅くするのが一番効く。
_HOST_INTERVAL = {}
MAX_INTERVAL = 6.0

# HOMES/アットホームは「1実行あたり最初の5〜6回だけ通し、超えるとIPごと
# ブロックして間隔を空けても解けない」仕様（Actions上で実測）。
# 予算内に収め、最初の202が出た時点で打ち切る。
_HOST_BUDGET = {"www.homes.co.jp": 5, "www.athome.co.jp": 5}
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
    with _BLOCK_LOCK:
        until = _BLOCKED_UNTIL.get(host, 0)
    if time.time() < until:
        return ""  # ブロック中。叩かない
    if not consume_budget(host):
        return ""
    for attempt in range(max_retry):
        with _Gate(host):
            html = fetch(url, impersonate=impersonate)
        # HOMESの202はfetch内でhtml=""になる。athomeの認証中ページは硬い200なので中身で判定
        if html and "認証中" not in html[:3000]:
            speed_up(host)
            return html
        if host in _HOST_BUDGET:
            # このホストは一度202が出たら間隔を空けても通らない。即打ち切る
            kill_budget(host)
            print(f"  {host}: ブロック検知、以降スキップ", file=sys.stderr)
            return ""
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


def collect_all():
    """駅ごとの取得を並列実行。同一サイトへの同時接続は WORKERS で抑える。"""
    all_items = []
    portal_count = Counter()

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(collect_station, st, cd): st for st, cd in STATIONS.items()}
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


def collect_station(station, codes):
    """1駅ぶんを取得して (items, portal_count, ログ行) を返す。"""
    all_items = []
    portal_count = Counter()
    log = []
    if True:
        # SUUMO (5種別: 中古3 + 新築2)
        for kind, path in [("mansion", f"ms/chuko/tokyo/ek_{codes['suumo']}/"),
                            ("house",   f"chukoikkodate/tokyo/ek_{codes['suumo']}/"),
                            ("land",    f"tochi/tokyo/ek_{codes['suumo']}/"),
                            # 新築戸建のみ追加。新築マンションは実測でほぼ通らず
                            # アクセス数だけ増えるので外す
                            ("house",   f"ikkodate/tokyo/ek_{codes['suumo']}/")]:
            # 恵比寿・広尾・代官山などは条件を満たす物件が極端に少ない。
            # 実測（恵比寿172件/広尾170件/代官山180件を全走査）で通過は1件だけだった。
            # 取りこぼしを無くすため、この駅だけ深いページまで見る。
            pages = (1, 2, 3, 4, 5, 6) if station in DEEP_SCAN_STATIONS else (1, 2, 3, 4)
            items = []
            for pn in pages:
                url = (f"https://suumo.jp/{path}?et=10&pn={pn}"
                       f"&kb={PRICE_MIN}&kt={PRICE_MAX}")
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
        HOMES_TARGETS = ("大井町", "戸越") + tuple(PRIORITY_STATIONS)
        use_homes = codes.get("homes") and station in HOMES_TARGETS
        for kind, path in ([] if not use_homes else
                           [("land", f"tochi/tokyo/{codes['homes']}/list/")]):
            items = []
            for pn in (1,):   # 予算5回に収めるため1ページのみ
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

        # アットホーム (3種別) — Cloudflare回避でcurl_cffi使用 + リトライ
        use_athome = codes.get("athome") and station in HOMES_TARGETS
        for kind, path in ([] if not use_athome else
                           [("land", f"tochi/tokyo/{codes['athome']}/list/")]):
            items = []
            for pn in (1,):   # 予算5回に収めるため1ページのみ
                url = f"https://www.athome.co.jp/{path}?page={pn}"
                html = fetch_with_retry(url, impersonate=True)
                page_items = parse_athome(html, station, kind)
                if not page_items:
                    break
                items.extend(page_items)
            kept = filter_with_walk_rescue(items)
            log.append(f"[アットホーム {kind}] {station}: parsed={len(items)} kept={len(kept)}")
            all_items.extend(kept)
            portal_count[f"アットホーム {kind}"] += len(kept)
            time.sleep(SLEEP_BETWEEN)

        # ノムコム
        for kind, path in ([] if not codes.get("nomu") else
                           [("mansion", f"mansion/{codes['nomu']}/"),
                            ("house",   f"house/{codes['nomu']}/"),
                            ("land",    f"land/{codes['nomu']}/")]):
            items = []
            for pn in (1, 2, 3):
                url = f"https://www.nomu.com/{path}?page={pn}"
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
                            ("house",   f"kounyu/kodate/{codes['livable']}/")]):
            items = []
            for pn in (1, 2, 3):
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
        for pn in (1, 2):   # 賃貸は最大の供給源なので2ページ取る
            url = f"https://suumo.jp/chintai/tokyo/ek_{codes['suumo']}/?page={pn}"
            html = fetch_with_retry(url)
            page = parse_suumo_rent(html, station)
            if not page:
                break
            rent_items.extend(page)
            time.sleep(SLEEP_BETWEEN)
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
        if (needs_walk or needs_addr) and passes_except_walk(it):
            use_cffi = it["source"] in ("HOMES", "アットホーム")
            html = fetch(it["url"], impersonate=use_cffi)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text(" ", strip=True)
                if needs_walk:
                    w = parse_walk(text, it["station"])
                    if w is not None:
                        it["walk"] = w
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
    if addr and not re.search(r"(品川区|渋谷区|目黒区|港区|大田区|新宿区|世田谷区|中央区|台東区)", addr):
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


def sort_for_notify(items):
    """優先駅を先頭に、駅ごとにまとめる。駅内は駐車場あり→駅近の順。"""
    order = {s: i for i, s in enumerate(PRIORITY_STATIONS)}
    return sorted(items, key=lambda it: (
        0 if it.get("_price_down") else 1,   # 値下げは最優先で見せる
        order.get(it.get("station"), 99),
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
            price_note = it.get("_price_note", "")
            parts = [price, layout, area, walk, age_str, pk_str, price_note, dup_str]
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
        print("在庫ページ用に全件の詳細を取得中…")
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
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
        _ALL_ITEMS = items
        import gen_page
        # 駅タブは左から優先順（恵比寿/目黒/中目黒 → 以降はSTATIONS定義順）
        order = PRIORITY_STATIONS + [s for s in STATIONS if s not in PRIORITY_STATIONS]
        n = gen_page.build(items, str(BASE_DIR / "docs" / "index.html"),
                           station_order=order)
        print(f"在庫ページ生成: {n}件 → docs/index.html")
    except Exception as e:
        print(f"在庫ページ生成に失敗: {e}", file=sys.stderr)
        _ALL_ITEMS = items
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
    price_now = mark_price_changes(items, state.get("prices", {}) or {})

    if not seen:
        print("初回実行: スナップショットのみ保存（通知なし）")
        save_state({"seen_ids": [it["id"] for it in items], "last_run": int(time.time()),
                    "prices": price_now})
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

    # 同じ日に2回通知しない（予備の実行が走っても二重送信しないため）
    from datetime import datetime, timezone, timedelta
    today_jst = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    prev = load_state()
    already = prev.get("notified_on") == today_jst
    forced = os.environ.get("DEBUG_FORCE_NOTIFY") == "1"
    # 手動実行(workflow_dispatch)は動作確認用。通知枠を消費しない。
    # 消費すると、その日の自動実行がスキップされて朝のメールが届かなくなる。
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if already and not forced:
        print(f"本日({today_jst})は通知済みのため送信をスキップ（ページのみ更新）")
    elif manual and not forced:
        print("手動実行のため通知はスキップ（自動実行の枠を残す。ページのみ更新）")
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
                "notified_on": prev.get("notified_on") if (already or (manual and not forced))
                               else today_jst})


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
