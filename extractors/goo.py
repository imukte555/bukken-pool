"""goo住宅 の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import re
from urllib.parse import urljoin, unquote
from bs4 import BeautifulSoup


def _goo_big(u: str, size: str = "800x800") -> str:
    """img.house.goo.ne.jp/ap/N/<encoded>?91x91 -> ?800x800。
    実測: ?800x800 は 200/image/jpeg で元画像の上限(LIFULL系は500px)まで拡大される。
    ページ内に実在する値は ?91x91(サムネ) / ?400x400(ギャラリー) / ?100x75(関連物件)。"""
    if "img.house.goo.ne.jp" in u:
        return re.sub(r"\?\d+x\d+$", "?" + size, u)
    return u


def goo_source_url(u: str) -> str:
    """プロキシURLの中に二重URLエンコードで入っている元画像URL（LIFULL 500px / SUUMO原寸）を取り出す。"""
    m = re.match(r"https?://img\.house\.goo\.ne\.jp/ap/\d+/([^?]+)", u)
    return unquote(unquote(m.group(1))) if m else u


def _clean(s: str) -> str:
    s = s.replace("　", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t\r\n]+", " ", s).strip()
    return s


def extract(soup: BeautifulSoup, base_url: str) -> dict:
    photos, plan, specs = [], None, {}
    seen = set()

    # 1) 写真＋間取り: #thumbs ul.thumbs li a.thumb  (href=?400x400 大判, img.src=?91x91 サムネ)
    #    a#slider_replace_madori が間取り図。alt="周辺環境" は近隣店舗の写真なので除外。
    for a in soup.select("#thumbs ul.thumbs li a.thumb"):
        img = a.find("img")
        href = a.get("href") or (img.get("src") if img else "")
        if not href:
            continue
        url = _goo_big(urljoin(base_url, href))
        alt = _clean(img.get("alt", "")) if img else ""
        title = _clean(a.get("title", ""))
        if a.get("id") == "slider_replace_madori" or alt == "間取り" or title == "間取り":
            plan = plan or url
            continue
        if alt == "周辺環境" or re.match(r"^【.+?】", title) or re.search(r"まで\d+ｍ|まで\d+m$", title):
            continue
        if url in seen:
            continue
        seen.add(url)
        photos.append(url)

    # 2) フォールバック: メイン画像のみ (JS無しでも入っている)
    if not photos:
        main = soup.find("img", id="slider_replace")
        if main and main.get("src"):
            photos.append(_goo_big(urljoin(base_url, main["src"])))

    # 3) 諸元: th/td。1行に th,td が2〜3組並ぶ (rent_detail_table_02)。
    #    detail_outline-data は要約（礼金・敷金が結合）なので、詳細表を後に読んで上書きする。
    tables = soup.select("table.detail_outline-data") + soup.select("table.rent_detail_table_02")
    for t in tables:
        for tr in t.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            i = 0
            while i < len(cells) - 1:
                if cells[i].name == "th" and cells[i + 1].name == "td":
                    th, td = cells[i], cells[i + 1]
                    # リンク文言（周辺地図/乗換案内/駅情報/近くの駐車場を探す 等）を落とす
                    for a in td.find_all("a"):
                        a.decompose()
                    label = _clean(th.get_text(" ", strip=True))
                    val = _clean(td.get_text(" ", strip=True))
                    if label:
                        specs[label] = val
                    i += 2
                else:
                    i += 1

    # 4) 取引態様・会社: お問い合わせ先 td 内に "取引態様:仲介" で入っている
    contact = soup.select_one("table.detail_table_kurashi td")
    if contact:
        ctext = _clean(contact.get_text(" ", strip=True))
        for key in ("商号", "免許番号", "取引態様", "TEL"):
            m = re.search(rf"{key}:\s*([^ ]+(?: [^ :]+)*?)(?= [^ :]+:|$)", ctext)
            if m:
                specs[key] = m.group(1).strip()

    # 5) おすすめポイント (dl.detail_recommend)
    rec = soup.select_one("dl.detail_recommend dd")
    if rec:
        specs["おすすめポイント"] = _clean(rec.get_text(" ", strip=True))

    return {"photos": photos, "plan": plan, "specs": specs}


# 実行確認（2026-10-10, 保存HTML3件）: photos 15/21/6枚, plan 3/3件, specs 59ラベル/件。
# スクリプト本体: /private/tmp/claude-501/-Users-sho/74c9d820-83eb-4979-b56d-f05b4597e1db/scratchpad/goo_extract.py

