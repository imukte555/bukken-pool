"""CHINTAI の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import copy
import re
from urllib.parse import urljoin, urlsplit, urlunsplit


def extract(soup, base_url):
    """CHINTAI (www.chintai.net/detail/bk-XXXX/) 詳細ページ → photos / plan / specs"""

    def abs_(u):
        u = (u or "").strip()
        if not u or u.startswith("data:"):
            return ""
        if u.startswith("//"):
            u = "https:" + u
        return urljoin(base_url, u)

    def full(u):
        # img.chintai.net の ?interpolation=...&fit=inside|468px:350px / &resize=600:* は
        # 配信側リサイズ。クエリを落とすと原寸(実測: 525x700)が返る。
        s = urlsplit(u)
        if "img.chintai.net" in s.netloc:
            return urlunsplit((s.scheme, s.netloc, s.path, "", ""))
        return u

    def img_url(img):
        # lazyload: src は 1x1 base64、実URLは data-original
        return full(abs_(img.get("data-original") or img.get("src")))

    photos, plan, seen = [], None, set()
    gal = soup.find(id="detail_galleryArea")
    if gal is not None:
        # 写真: #gaLargeA 内のカルーセル。同じ写真が 468px(lazyload)/600px(modal)/80px(thumb) で
        # 3回出るので path で重複排除。周辺環境(#ancMap)・店舗写真(awsorg/shop)は別領域なので混ざらない。
        cands = gal.select("#gaLargeA ul.js_gallery_carousel_view img") or gal.select("#gaLargeA img")
        for img in cands:
            u = img_url(img)
            if u and u not in seen and "/awsorg/shop/" not in u:
                seen.add(u)
                photos.append(u)
        # 間取り図: #gaLargeB (alt="間取り図")
        p = gal.select_one("#gaLargeB img") or gal.find("img", alt="間取り図")
        if p is not None:
            plan = img_url(p) or None

    # ---------- specs ----------
    specs = {}

    def norm(s):
        s = s.replace("\xa0", " ")
        return re.sub(r"\s+", " ", s).strip()

    def norm_label(s):
        # "敷金 / 保証金" → "敷金/保証金"
        return re.sub(r"\s*/\s*", "/", norm(s))

    def td_text(td):
        td = copy.copy(td)
        # 用語解説の吹き出し(display:none)・「閉じる」・地図リンク・注記(p.small)・メリットプランのリンクを除去
        for x in td.select("div.js_help_baloon, dl.spec_description, p.close, span.mapTextLink, p.small, a[href^='/meritplan']"):
            x.decompose()
        # 設備名が <a class="js_help_btn"> に入っている → タグだけ外して文字は残す
        for a in td.select("a.js_help_btn"):
            a.unwrap()
        # 設備・条件: <span class="js_help">項目</span> / <span class="js_help">項目</span> … の列挙
        helps = td.select("span.js_help")
        if len(helps) >= 2:
            items = [norm(h.get_text(" ", strip=True)) for h in helps]
            return " / ".join(i for i in items if i)
        # 交通: dl(dt=路線/駅, dd=徒歩N分) を1行ずつ
        dls = td.select("div.mod_necessaryTime dl")
        if dls:
            out = []
            for dl in dls:
                dt = dl.find("dt")
                dd = dl.find("dd")
                a = re.sub(r"\s+", "", dt.get_text(" ", strip=True)) if dt else ""
                b = re.sub(r"\s+", "", dd.get_text(" ", strip=True)) if dd else ""
                out.append(f"{a} {b}".strip())
            return "、".join(out)
        lis = td.select("div.payment_info li")
        if lis:
            items = [norm(li.get_text(" ", strip=True)) for li in lis]
            rest = td.select_one("div.payment_info p")
            if rest is not None:
                items.append(norm(rest.get_text(" ", strip=True)))
            return " / ".join(i for i in items if i)
        return norm(td.get_text(" ", strip=True))

    def walk_table(table):
        for tr in table.find_all("tr"):
            # ネストした nestTbl の tr は親 table の find_all にも含まれるので除外
            if tr.find_parent("table") is not table:
                continue
            cells = [c for c in tr.find_all(["th", "td"], recursive=False)]
            i = 0
            while i < len(cells):
                c = cells[i]
                if c.name != "th":
                    i += 1
                    continue
                label = norm_label(c.get_text(" ", strip=True))
                td = cells[i + 1] if i + 1 < len(cells) and cells[i + 1].name == "td" else None
                i += 2
                if not label or td is None:
                    continue
                nest = td.find("table", class_="nestTbl")
                if nest is not None:
                    # 家賃 td の中に 管理費等 / 敷金・保証金 / 礼金・償却 が入っている
                    rent = td.select_one("span.rent")
                    if rent is not None:
                        specs.setdefault(label, norm(rent.get_text("", strip=True)))
                    for ntr in nest.find_all("tr"):
                        ncells = ntr.find_all(["th", "td"], recursive=False)
                        for j, nc in enumerate(ncells):
                            if nc.name == "th" and j + 1 < len(ncells) and ncells[j + 1].name == "td":
                                specs.setdefault(norm_label(nc.get_text(" ", strip=True)), td_text(ncells[j + 1]))
                    continue
                val = td_text(td)
                if val:
                    specs.setdefault(label, val)

    # 物件概要(家賃〜物件階層) と こだわり設備・特徴/条件/入居時期 の表だけ。
    # div.detail_otherList(同じ建物の空室一覧) と 問い合わせフォームの table は対象外。
    for table in soup.select("div.detail_basicInfo table, div.detail_specTable table"):
        if table.find_parent("table") is not None:
            continue
        walk_table(table)

    # 物件名: 「CREDO MEGURO B1-1階／東京都…の賃貸物件詳細」の「／」前
    h2 = soup.select_one("div#article h2") or soup.find("h2")
    if h2 is not None:
        name = norm(h2.get_text(" ", strip=True))
        name = name.split("／")[0].strip()
        if name:
            specs.setdefault("物件名", name)

    return {"photos": photos, "plan": plan, "specs": specs}


