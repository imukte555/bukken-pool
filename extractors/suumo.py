"""SUUMO の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import re
import html as _html
from urllib.parse import urljoin, unquote

from bs4 import BeautifulSoup, NavigableString

# ---- 画像 ---------------------------------------------------------------
# 周辺環境カテゴリ(SUUMOの data-category / orgn の末尾)。物件そのものの写真ではない
SURROUND_CATS = {
    "コンビニ", "スーパー", "ドラッグストア", "ショッピングセンター", "郵便局", "銀行",
    "病院", "公園", "駅", "小学校", "中学校", "高校", "幼稚園・保育園", "大学・短大・専門学校",
    "図書館", "役所", "飲食店", "ホームセンター", "その他環境写真",
}
_SKIP_IMG = ("No_Photo", "spacer.gif", "krcommon", "gazo/kaisha", "/jjcommon/")
_BLOCK = {"div", "p", "li", "ul", "ol", "tr", "table", "dl", "dt", "dd", "h1", "h2", "h3", "h4"}
_SKIP_TAGS = {"script", "style", "input", "form", "select", "option", "noscript"}


def _unwrap(u: str, base_url: str) -> str:
    """img01.suumo.com/jj/resizeImage?src=gazo%2F...&w=NNN → https://suumo.jp/front/gazo/... (原寸)"""
    if not u:
        return ""
    u = _html.unescape(_html.unescape(u)).strip()
    m = re.search(r"[?&]src=([^&,]+)", u)
    if m and "resizeImage" in u:
        raw = unquote(m.group(1))
        if raw.startswith("http"):
            return raw
        return "https://suumo.jp/front/" + raw.lstrip("/")
    return urljoin(base_url, u)


def _photo_key(u: str) -> str:
    m = re.search(r"/(\d+_\d{4}\.(?:jpg|jpeg|png|gif))", u, re.I)
    return m.group(1) if m else u


def _is_property_photo(u: str) -> bool:
    return "gazo/bukken" in u and not any(s in u for s in _SKIP_IMG)


def _plain(s: str) -> str:
    """属性値に入った <sup>2</sup> 等を落とす"""
    s = _html.unescape(s or "")
    s = re.sub(r"<sup>\s*2\s*</sup>", "²", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _collect_images(soup, base_url):
    """[(url, category, caption, source_id)] を文書順で。重複は呼び元で落とす"""
    out = []
    # (A) 新テンプレの大カルーセル(写真20枚超などで出る)。原寸URLが data-src に直接入っている
    for a in soup.select("a.js-lightboxItem[data-src]"):
        out.append((_unwrap(a["data-src"], base_url), _plain(a.get("data-category")),
                    _plain(a.get("data-caption")), "carousel"))
    # (B) サムネ→拡大(thickbox)用の hidden input。全テンプレに存在する。
    #     value='<resizeImage URL>&w=500,<カテゴリ>'。キャプションは同じ箱の p.bw
    for inp in soup.select("input[type=hidden][id$=orgn]"):
        val = _html.unescape(inp.get("value") or "")
        url_part, _, cat = val.partition(",")
        cap = ""
        box = inp.find_parent("div", class_="thickbox_contents_section")
        if box is not None:
            p = box.select_one("p.bw")
            if p is not None:
                cap = _plain(p.get_text(" ", strip=True))
        out.append((_unwrap(url_part, base_url), cat.strip(), cap, inp.get("id", "")))
    # (C) 保険: ギャラリー内のサムネだけ(遅延読み込みは rel 属性に実URL)。
    #     ページ内の他の img(担当者写真・プレゼント画像・購入サポート画像)は拾わない
    for img in soup.select("a.jscNyroModal img, .carousel_navlist_thumb-image-inner img, .carousel_item img"):
        u = img.get("rel") or img.get("data-src") or img.get("src") or ""
        if isinstance(u, list):
            u = u[0] if u else ""
        out.append((_unwrap(u, base_url), "", _plain(img.get("alt")), "img"))
    return out


# ---- 諸元 ---------------------------------------------------------------
_STRIP_LINKS = {"支払シミュレーション", "乗り換え案内", "周辺環境", "ヒント"}
_SKIP_LABELS = {"関連リンク", "不動産会社ガイド", "担当者より"}
# 任意: 呼び元が使いやすい共通キーを、無い時だけ足す
_ALIASES = {
    "築年月": ("完成時期(築年月)", "完成時期", "築年月"),
    "入居時期": ("引渡可能時期", "引き渡し時期", "入居時期", "引渡時期"),
    "所在地": ("所在地", "住所"),
    "構造・階建": ("構造・階建て", "所在階/構造・階建", "構造・階建"),
}


def _text(el) -> str:
    """ブロック要素/brを改行に、インラインは連結。<sup>2</sup>→²。
    注意: SUUMOのHTMLは html.parser だと <br> の中に後続要素が入る(入れ子になる)ので
    br で return せず必ず子を辿る。"""
    parts = []

    def walk(n):
        if isinstance(n, NavigableString):
            parts.append(str(n))
            return
        if n.name in _SKIP_TAGS:
            return
        if n.name == "sup" and n.get_text(strip=True) in ("2", "２"):
            parts.append("²")
            return
        if n.name == "br" or n.name in _BLOCK:
            parts.append("\n")
        for c in n.children:
            walk(c)
        if n.name in _BLOCK:
            parts.append("\n")

    walk(el)
    txt = "".join(parts)
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in txt.split("\n")]
    lines = [re.sub(r"\[\s*[□■]?\s*\]", "", ln).strip() for ln in lines]  # リンクを消した後の空の [ ]
    seen, out = set(), []
    for ln in lines:
        if ln and ln not in seen:
            seen.add(ln)
            out.append(ln)
    return "\n".join(out)


def _clean_cell(cell):
    for a in cell.find_all("a"):
        if a.get_text(strip=True) in _STRIP_LINKS:
            a.decompose()
    return _text(cell)


def _norm_label(s: str) -> str:
    s = s.replace("（", "(").replace("）", ")").replace("･", "・").replace("ヒント", "")
    s = re.sub(r"\s+", "", s)
    return s.rstrip(":：")


def _put(specs: dict, k: str, v: str):
    if not k or not v:
        return
    cur = specs.get(k)
    if cur is None or cur in ("-", "") or (v != "-" and v.startswith(cur) and len(v) > len(cur)):
        specs[k] = v


def extract(soup: BeautifulSoup, base_url: str) -> dict:
    """SUUMO 売買(中古マンション/一戸建て/土地)詳細ページ用。
    返り値: {"photos": [原寸URL...], "plan": 間取り図URL|None, "specs": {ラベル: 値},
             "photos_surround": [周辺環境写真URL...], "photo_meta": [{url,category,caption}]}
    photos は物件そのもの(外観・室内・共用部)。周辺施設の写真は photos_surround に分ける。"""
    photos, surround, plan, meta, seen = [], [], None, [], set()
    for url, cat, cap, src in _collect_images(soup, base_url):
        if not _is_property_photo(url):
            continue
        key = _photo_key(url)
        if key in seen:
            continue
        seen.add(key)
        meta.append({"url": url, "category": cat, "caption": cap})
        if cat == "間取り図" or (not cat and re.search(r"間取|区画図", cap)):
            if plan is None:
                plan = url
            continue
        is_surround = (cat in SURROUND_CATS) or src.startswith("imgShuhenKankyo") \
            or bool(re.search(r"まで\d+(\.\d+)?m", cap))
        (surround if is_surround else photos).append(url)

    specs = {}
    main = soup.select_one("#mainContents") or soup
    h1 = soup.select_one("h1")
    if h1 is not None:
        _put(specs, "タイトル", re.sub(r"\s+", " ", h1.get_text(" ", strip=True)))

    # 物件概要: #mainContents 内の <table summary="表">。th/td 形式で 1行に (th,td) が2組。
    # 「担当者より」「お問い合せ先」は th 1つに td 2つ → th ごとに td をまとめる
    for table in main.select('table[summary="表"]'):
        for tr in table.find_all("tr"):
            groups, cur = [], None
            for cell in tr.find_all(["th", "td"], recursive=False):
                if cell.name == "th":
                    fl = cell.select_one("div.fl")  # ラベル本体。右側の「ヒント」リンクを除く
                    cur = [_norm_label((fl or cell).get_text(" ", strip=True)), []]
                    groups.append(cur)
                elif cur is not None:
                    cur[1].append(cell)
            for label, tds in groups:
                if not label or label in _SKIP_LABELS or not tds:
                    continue
                v = "\n".join(t for t in (_clean_cell(td) for td in tds) if t)
                _put(specs, label, v)

    # 会社情報の dl.cf: 免許番号 / 取引態様
    for dt in main.select("dl.cf dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            _put(specs, _norm_label(dt.get_text(strip=True)), dd.get_text(" ", strip=True))
    if "取引態様" not in specs:
        m = re.search(r"取引態様[：:]\s*＜?([^＞\s<]+)", main.get_text(" ", strip=True))
        if m:
            specs["取引態様"] = m.group(1)

    # 特徴ピックアップ(設備・特徴タグ、" / " 区切り): h3 を包む secTitleOuter* の次の div.mt10
    for h3 in main.find_all("h3"):
        if "特徴ピックアップ" in h3.get_text():
            outer = h3.find_parent("div", class_=re.compile("secTitleOuter")) or h3
            nxt = outer.find_next_sibling("div")
            if nxt is not None:
                _put(specs, "設備・特徴", re.sub(r"\s+", " ", nxt.get_text(" ", strip=True)))
            break

    # 物件の特徴: キャッチ(h3.fs16) + 本文(p.fs14)。会社のアピール文
    appeal = []
    for h3 in main.select("h3.fs16"):
        p = h3.find_next("p", class_="fs14")
        appeal.append(h3.get_text(" ", strip=True) + ("\n" + _text(p) if p is not None else ""))
    if appeal:
        _put(specs, "アピール", "\n".join(appeal))

    # 備考相当を1キーにまとめる
    bik = [specs[k] for k in ("その他制限事項", "その他概要・特記事項") if specs.get(k) and specs[k] != "-"]
    if bik:
        _put(specs, "備考", "\n".join(bik))

    for canon, cands in _ALIASES.items():
        if canon not in specs:
            for c in cands:
                if specs.get(c) and specs[c] != "-":
                    specs[canon] = specs[c]
                    break

    # 土地など物件の写真が無いときは周辺環境写真を使う
    if not photos and surround:
        photos = list(surround)
    return {"photos": photos, "plan": plan, "specs": specs,
            "photos_surround": surround, "photo_meta": meta}


# 使い方:
#   import sys; sys.path.insert(0, '/Users/sho/code/bukken-pool'); from watcher import fetch
#   html = fetch(url, impersonate=True)
#   r = extract(BeautifulSoup(html, 'html.parser'), url)
# 実測(2026-10-10): nc_21369582 → photos 18 / plan あり / specs 49キー
#                   nc_20837457 → photos 19 / plan あり / specs 49キー
#                   nc_21811299(土地) → photos 0(No_Photo) / surround 7 / plan None / specs 35キー


