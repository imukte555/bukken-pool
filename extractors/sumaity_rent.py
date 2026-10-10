"""スマイティ賃貸 の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import re
from urllib.parse import urljoin, urlparse, parse_qs, unquote
from bs4 import BeautifulSoup


def _orig(u):
    """k-img.com の optimized/cachedimg ラッパーから元画像URLを取り出す(無ければそのまま)"""
    if not u:
        return None
    if "k-img.com" in u and "img_path=" in u:
        q = parse_qs(urlparse(u).query)
        p = q.get("img_path", [None])[0]
        if p:
            return unquote(p)
    return u


def extract(soup, base_url):
    photos, plan = [], None
    seen = set()
    # ギャラリー本体: a.js-image-viewer[href] (1300x840 の大判)。img は lazy で
    # src=thumb_loader.gif なので href か img[data-original] を使う。
    for a in soup.select("a.js-image-viewer"):
        img = a.find("img")
        u = a.get("href") or (img and (img.get("data-original") or img.get("src")))
        if not u or "thumb_loader" in u or "no-image" in u:
            continue
        u = urljoin(base_url, u)
        alt = (img.get("alt") if img else "") or a.get("title", "")
        big = _orig(u)  # 元画像(SUUMO配信の _go/_co/_1o...jpg)
        if big in seen:
            continue
        seen.add(big)
        title = a.get("title", "")
        if "間取" in alt or re.search(r"_co\.jpg", big):
            plan = plan or big
        elif "周辺" in title or re.search(r"_s\d+o\.jpg", big):
            continue  # 周辺施設(スーパー等)の写真。物件写真ではない
        else:
            photos.append(big)
    if not photos and not plan:
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            photos.append(_orig(og["content"]))

    specs = {}
    # 諸元は th/td の table。p-estate-summary__table(概要) と p-estate-spec-table(詳細)。
    # 1行に th,td,th,td の2組が並ぶ行があるので th と td を順に対応させる。
    for tbl in soup.select("table.p-estate-summary__table, table.p-estate-spec-table"):
        # 不動産会社ブロック(営業時間/免許番号…)は取引態様だけ拾う
        is_company = bool(tbl.find("th", string=re.compile("営業時間|免許番号")))
        for tr in tbl.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            i = 0
            while i < len(cells) - 1:
                th, td = cells[i], cells[i + 1]
                if th.name != "th" or td.name != "td":
                    i += 1
                    continue
                label = th.get_text(" ", strip=True)
                # td 内の問い合わせ誘導リンク(「〜を聞いてみる」等)と地図ボタンを除く
                for a in td.find_all("a") + td.select(".c-button, .js-modal-open"):
                    a.decompose()
                val = re.sub(r"\s+", " ", td.get_text(" ", strip=True)).replace("m 2", "m²")
                if is_company and label != "取引態様":
                    i += 2
                    continue
                if label and val and label not in specs:
                    specs[label] = val
                i += 2
    return {"photos": photos, "plan": plan, "specs": specs}

