"""ハウスコム の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup


# 物件写真ではない画像（除外）
_EXCLUDE_PAT = re.compile(
    r"/img/spot/|/image/tag/|/shop/img/|/img/logo/|\.svg(\?|$)", re.I
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def extract(soup: BeautifulSoup, base_url: str) -> dict:
    """ハウスコム (www.housecom.jp/room_XXXX/) の詳細ページ。
    返り値: {"photos": [...], "plan": str|None, "specs": {label: value}}
    """
    photos, plan, specs = [], None, {}
    seen = set()

    def add_photo(url: str, label: str):
        nonlocal plan
        if not url:
            return
        url = urljoin(base_url, url)
        if _EXCLUDE_PAT.search(url):
            return  # 周辺スポット/タグ/店舗/ロゴ
        label = _norm(label)
        if "周辺" in label:
            return  # 駅・学校など周辺画像（複数物件で共用）
        if "間取" in label:
            if plan is None:
                plan = url
            return  # 間取図は photos に入れない
        if url in seen:
            return
        seen.add(url)
        photos.append(url)

    # 1) メインスライダー (alt="1/30 外観" のように 連番+種別)
    for img in soup.select("ul.room-main-slider li img"):
        add_photo(img.get("src"), re.sub(r"^\d+/\d+\s*", "", img.get("alt") or ""))

    # 2) 予備: JSON-LD Product.image（name末尾が種別。"～の外観" など）
    if not photos:
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(sc.string or "")
            except Exception:
                continue
            if isinstance(d, dict) and d.get("@type") == "Product":
                for im in d.get("image") or []:
                    if isinstance(im, dict):
                        add_photo(im.get("url"), (im.get("name") or "").split("の")[-1])
                    elif isinstance(im, str):
                        add_photo(im, "")

    # 3) 間取図: 専用ブロック div.room-madori img
    if plan is None:
        img = soup.select_one("div.room-madori img")
        if img and img.get("src"):
            plan = urljoin(base_url, img["src"])

    # 4) 諸元: table.detail_info (th>h3 / td) が3枚（部屋概要・契約情報・建物情報）
    for table in soup.select("table.detail_info"):
        for tr in table.find_all("tr"):
            th, td = tr.find("th"), tr.find("td")
            if not th or not td:
                continue
            label = re.sub(r"\s+", "", th.get_text(" ", strip=True))
            # td内の問い合わせリンク「初期費用を知りたい」等を除去
            for a in td.select("a.c--link-cv"):
                a.decompose()
            ul = td.find("ul", class_="detail_info_station")
            if ul:
                value = " / ".join(_norm(li.get_text(" ", strip=True)) for li in ul.find_all("li"))
            else:
                value = _norm(td.get_text(" ", strip=True))
            value = re.sub(r"\s*/\s*", " / ", value) if label in ("敷金/礼金", "保証金/敷引(償却)") else value
            if label and value and label not in specs:
                specs[label] = value

    # 5) 間取ブロックの補足 (間取り：1LDK / 間取詳細 / 専有面積 / 方位 / 部屋位置)
    for p in soup.select("div.room-madori_info p"):
        t = _norm(p.get_text(" ", strip=True))
        if "：" in t:
            k, v = t.split("：", 1)
            specs.setdefault(k.strip(), v.strip())

    # 6) 設備 (#equip .room-equip > div > h3 + div>p…) → "設備/カテゴリ": "a、b、c"
    for blk in soup.select("#equip div.room-equip > div"):
        h3 = blk.find("h3")
        if not h3:
            continue
        items = [_norm(p.get_text()) for p in blk.select("div p") if _norm(p.get_text())]
        if items:
            specs[f"設備/{_norm(h3.get_text())}"] = "、".join(items)

    # 7) 特徴の説明文 (#tag .room-tag-info h3 + p)
    for blk in soup.select("#tag .room-tag-info > div"):
        h3, p = blk.find("h3"), blk.find("p")
        if h3 and p:
            key = re.sub(r"\s+", "", h3.get_text())
            specs["特徴/" + key] = _norm(p.get_text(" ", strip=True))

    # 8) JSON-LD から 情報公開日・取扱店舗
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(sc.string or "")
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            if d.get("releaseDate"):
                specs.setdefault("情報公開日", d["releaseDate"])
            seller = (d.get("offers") or {}).get("seller") or {}
            if seller.get("name"):
                specs.setdefault("取扱店舗", seller["name"])
            if seller.get("telephone"):
                specs.setdefault("店舗電話", seller["telephone"])
            break

    return {"photos": photos, "plan": plan, "specs": specs}


if __name__ == "__main__":
    import sys
    for path, url in zip(sys.argv[1::2], sys.argv[2::2]):
        with open(path, encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        out = extract(soup, url)
        print("=" * 20, url)
        print("photos:", len(out["photos"]))
        for u in out["photos"]:
            print("  ", u)
        print("plan:", out["plan"])
        for k, v in out["specs"].items():
            print(f"  {k} = {v[:120]}")


