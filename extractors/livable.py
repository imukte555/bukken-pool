"""リバブル の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import re
from urllib.parse import urlparse, parse_qs, urljoin
from bs4 import BeautifulSoup

IMG_PROXY = "img.livable.co.jp"

def _unwrap(u, base_url):
    """img.livable.co.jp/?url=<enc>&w=768&h=576 → 中の原寸URL(クエリ無し)。"""
    if not u:
        return None
    u = urljoin(base_url, u.replace("&amp;", "&"))
    p = urlparse(u)
    if p.netloc == IMG_PROXY:
        inner = parse_qs(p.query).get("url", [None])[0]
        if inner:
            u = inner
    return u.split("?")[0]

def extract(soup, base_url):
    photos, plan, seen = [], None, set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "rue_image" not in src:  # 物件画像は全部 /rue_image/ 配下。店舗写真(eigyosho_image)・UI画像を除外
            continue
        u = _unwrap(src, base_url)
        if not u or u in seen:
            continue
        seen.add(u)
        if "/rue_image/layout/" in u:
            plan = plan or u
        elif re.search(r"/rue_image/(photo|misc\d+)/", u):
            photos.append(u)
    specs = {}
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if not dd:
            continue
        label = dt.get_text(" ", strip=True).rstrip("：:").strip()
        for sup in dd.find_all("sup"):
            sup.string = "^" + sup.get_text()  # m<sup>2</sup> → m^2
        val = re.sub(r"\s+", " ", dd.get_text(" ", strip=True))
        if label and val and label not in specs:
            specs[label] = val
    return {"photos": photos, "plan": plan, "specs": specs}

# 実行確認: photos 35件, plan=.../rue_image/layout/C48262241.gif, specs 29ラベル
# スクリプト: /private/tmp/claude-501/-Users-sho/74c9d820-83eb-4979-b56d-f05b4597e1db/scratchpad/scripts/livable_extract.py

