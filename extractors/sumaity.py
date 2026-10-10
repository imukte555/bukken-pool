"""スマイティ の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import re
from urllib.parse import urlparse, parse_qs, unquote
from bs4 import BeautifulSoup

PLAN_ALTS = {"間取り"}
SKIP_ALTS = {"周辺環境"}  # 近隣施設(郵便局・小学校・スーパー等)の写真。物件写真ではない


def _big(url):
    """sumaity.k-img.com/cachedimg/?img_path=<元画像URL>&width=..&height=.. から
    元画像(realestate-pctr.c.yimg.jp)のURLを取り出す。サムネ(width=0)も同じ元画像になる。"""
    if not url:
        return None
    if "cachedimg" in url:
        q = parse_qs(urlparse(url).query)
        if q.get("img_path"):
            url = unquote(q["img_path"][0])
    if url.endswith(".svg") or "/no_image/" in url.split("?")[0]:
        return None
    return url


def _cell_text(td):
    td = BeautifulSoup(str(td), "html.parser")  # 元soupを壊さないためコピー
    for junk in td.select("button, a.c-button, .js-btn-inquiry, script, style"):
        junk.decompose()
    for sup in td.find_all("sup"):
        sup.replace_with("^" + sup.get_text(strip=True))  # m<sup>2</sup> -> m^2
    txt = td.get_text(" ", strip=True)
    txt = re.sub(r"\s+", " ", txt).replace("m ^2", "m^2").replace("m^2", "m2")
    return txt.replace(" 地図・周辺情報", "").strip()


def extract(soup, base_url):
    photos, plan, seen = [], None, set()
    # 1) メインギャラリー <div class="js-estate-detail-gallery"><a class="js-image-viewer" href=大画像><img alt=種別>
    gal = soup.select_one(".js-estate-detail-gallery") or soup
    for a in gal.select("a.js-image-viewer"):
        img = a.find("img")
        alt = (img.get("alt") or "").strip() if img else ""
        u = _big(a.get("href") or (img and (img.get("data-original") or img.get("src"))))
        if not u or u in seen or alt in SKIP_ALTS:
            continue
        seen.add(u)
        if alt in PLAN_ALTS:
            plan = plan or u
        else:  # 外観 / 室内 / その他画像
            photos.append(u)
    # 2) 補助: 室内写真ビューア (ギャラリー外にある分も拾う)
    for a in soup.select("a.js-estate-detail-viewer-room-photo"):
        u = _big(a.get("href"))
        if u and u not in seen:
            seen.add(u)
            photos.append(u)
    # 3) 間取り図フォールバック: 特徴欄の間取り画像 (この住戸のもの)
    if not plan:
        img = soup.select_one(".p-estate-detail-features-layout__image img")
        if img:
            plan = _big(img.get("data-original") or img.get("src"))
    # 4) 諸元: table.p-estate-spec-table の th/td。1行に th,td,th,td が並ぶ行あり
    specs = {}
    for tbl in soup.select("table.p-estate-spec-table"):
        ths = {th.get_text(strip=True) for th in tbl.find_all("th")}
        if "学校" in ths and "買い物" in ths:
            continue  # 周辺施設表
        broker = "免許番号" in ths or "営業時間" in ths  # 取扱会社表
        for tr in tbl.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            for i, c in enumerate(cells):
                if c.name != "th" or i + 1 >= len(cells) or cells[i + 1].name != "td":
                    continue
                k = c.get_text(" ", strip=True)
                if broker and k != "取引態様":
                    continue
                v = _cell_text(cells[i + 1])
                if k and v and k not in specs:
                    specs[k] = v
    return {"photos": photos, "plan": plan, "specs": specs}


# 使い方:
# import sys; sys.path.insert(0, '/Users/sho/code/bukken-pool'); from watcher import fetch
# html = fetch(url, impersonate=True); r = extract(BeautifulSoup(html, 'html.parser'), url)
# 実測: prop_20859015 -> photos 5, plan あり, specs 45キー / prop_20935547 -> photos 28, plan あり

