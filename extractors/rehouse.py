"""三井のリハウス の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import json, re
from bs4 import BeautifulSoup

IMG_HOST = "img2-direct.miraie-net.com"
PLAN_WORDS = ("間取", "区画図", "プラン")

def _payload_images(soup):
    """Nuxt devalue payload から {url, title, comment} を全部取り出す（関連物件分も含む）"""
    out = []
    for sc in soup.find_all("script", type="application/json"):
        txt = sc.string or ""
        if IMG_HOST not in txt or not txt.lstrip().startswith("["):
            continue
        try:
            arr = json.loads(txt)
        except Exception:
            continue
        def res(i):
            return arr[i] if isinstance(i, int) and 0 <= i < len(arr) else None
        for node in arr:
            if isinstance(node, dict) and {"url", "title", "comment"} <= set(node):
                u = res(node["url"])
                if isinstance(u, str) and IMG_HOST in u:
                    t = res(node["title"]); c = res(node["comment"])
                    out.append({"url": u, "title": t if isinstance(t, str) else "",
                                "comment": c if isinstance(c, str) else ""})
    return out

def extract(soup, base_url):
    photos, plan, specs = [], None, {}

    # 1) この物件の画像ディレクトリをヒーロー画像から決める
    hero = [img.get("src") or img.get("data-src") or ""
            for img in soup.select('[class*="property-detail-hero"] img')]
    hero = [h for h in hero if IMG_HOST in h]
    dirs = {re.sub(r"/[^/]+$", "/", h.replace("/orgt/", "/org/")) for h in hero}
    if not dirs:
        og = soup.find("meta", property="og:image")
        if og and IMG_HOST in (og.get("content") or ""):
            dirs = {re.sub(r"/[^/]+$", "/", og["content"].replace("/orgt/", "/org/"))}

    # 2) payload の原寸(/org/)URL を同ディレクトリで絞る
    seen = set()
    for im in _payload_images(soup):
        u = im["url"]
        if not any(u.startswith(d) for d in dirs) or u in seen:
            continue
        seen.add(u)
        label = f'{im["title"]} {im["comment"]}'
        if plan is None and any(w in label for w in PLAN_WORDS):
            plan = u
        else:
            photos.append(u)

    # 3) payload が取れない時: ヒーロー img から原寸版を作る
    if not photos and not plan:
        for h in hero:
            u = re.sub(r"_(pcViewLargerHeight|spLongWidth|pcLongHight|spLongHight)\.jpg$", ".jpg",
                       h.replace("/orgt/", "/org/"))
            if u in seen:
                continue
            seen.add(u)
            img = soup.find("img", src=h)
            alt = (img.get("alt") or "") if img else ""
            if plan is None and any(w in alt for w in PLAN_WORDS):
                plan = u
            else:
                photos.append(u)

    # 4) 諸元: tr.table-row > td.table-header / td.table-data
    for tr in soup.select("tr.table-row"):
        th = tr.select_one("td.table-header")
        td = tr.select_one("td.table-data")
        if not th or not td:
            continue
        k = th.get_text(" ", strip=True)
        v = td.get_text("\n", strip=True)
        if k and k not in specs:
            specs[k] = v
    # 予備: 要約欄 div.summary-row > span.summary-label + 次要素
    for row in soup.select("div.summary-row"):
        lab = row.select_one(".summary-label")
        if not lab:
            continue
        k = lab.get_text(" ", strip=True)
        val = lab.find_next_sibling()
        if k and val and k not in specs:
            specs[k] = val.get_text(" ", strip=True)

    return {"photos": photos, "plan": plan, "specs": specs}

# 実行結果: FXC7DA09 → photos 16, plan 369268939.jpg(区画図), specs 22ラベル
#           FXC7WA0B → photos 8,  plan 369056511.jpg, specs 24ラベル
# 保存先: /private/tmp/claude-501/-Users-sho/74c9d820-83eb-4979-b56d-f05b4597e1db/scratchpad/rehouse_extract.py

