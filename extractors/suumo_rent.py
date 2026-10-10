"""SUUMO賃貸 の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup


def extract(soup, base_url):
    """SUUMO賃貸 詳細ページ (suumo.jp/chintai/jnc_XXXX/?bc=YYYY) 用。
    photos: ギャラリー大画像 (_o.jpg)。間取り図・周辺施設(_sNo)・店舗/広告(front_kaisha)は除外。
    plan:   間取り図 (_co.jpg)。
    specs:  ヘッダ賃料ブロック + property_view_table + table_gaiyou + 設備リスト。
    """
    photos, plan = [], None
    seen = set()
    gallery = soup.select("ul#js-view_gallery-list img.property_view_object-img") \
        or soup.select("img.property_view_object-img")
    for img in gallery:
        src = img.get("data-src") or img.get("src") or ""
        if not src:
            continue
        src = urljoin(base_url, src)
        if "/front_kaisha/" in src or src in seen:
            continue  # 店舗写真・不動産会社の広告バナー
        seen.add(src)
        alt = (img.get("alt") or "").strip()
        cat = alt.split("　")[0]  # alt = "カテゴリ　建物名"
        fname = src.rsplit("/", 1)[-1]
        if cat == "間取り図" or re.search(r"_c[ot]\.jpg$", fname):
            plan = plan or src
            continue
        if re.search(r"_s\d+[ot]\.jpg$", fname):
            continue  # 周辺施設（スーパー/コンビニ等）の写真。物件写真ではない
        photos.append(src)

    specs = {}

    def put(k, v):
        k = re.sub(r"\s+", "", k)
        v = re.sub(r"[ \t\r\n　]+", " ", v).strip()
        v = v.replace("m 2", "m²")
        if k and v and k not in specs:
            specs[k] = v

    # 賃料・管理費・敷金・礼金・保証金・敷引 (ヘッダ)
    note = soup.select_one(".property_view_note")
    if note:
        em = note.select_one(".property_view_note-emphasis")
        if em:
            put("賃料", em.get_text(strip=True))
        for sp in note.select(".property_view_note-list span"):
            t = sp.get_text(" ", strip=True)
            if ":" in t or "：" in t:
                k, v = re.split(r"[:：]", t, 1)
                put(k, v)

    # 概要テーブル2つ (th/td が1行に2組並ぶ)
    for tbl in soup.select("table.property_view_table, table.table_gaiyou"):
        for tr in tbl.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            i = 0
            while i + 1 < len(cells):
                if cells[i].name == "th" and cells[i + 1].name == "td":
                    put(cells[i].get_text(" ", strip=True),
                        cells[i + 1].get_text(" ", strip=True))
                    i += 2
                else:
                    i += 1

    # 部屋の特徴・設備 (読点区切りの1つのli)
    opt = soup.select_one("#bkdt-option ul.inline_list")
    if opt:
        put("設備・特徴", opt.get_text(" ", strip=True))

    # 建物名
    h1 = soup.select_one("h1.section_h1-header-title")
    if h1:
        put("建物名", h1.get_text(strip=True))

    return {"photos": photos, "plan": plan, "specs": specs}


# 使い方:
#   import sys; sys.path.insert(0, '/Users/sho/code/bukken-pool'); from watcher import fetch
#   html = fetch(url, impersonate=True)
#   extract(BeautifulSoup(html, "html.parser"), url)
# 実測: 3ページとも photos=13, plan=…_co.jpg, specs 38キー
#   (保存HTML: /private/tmp/claude-501/-Users-sho/74c9d820-83eb-4979-b56d-f05b4597e1db/scratchpad/suumo_rent/page{0,1,2}.html)

