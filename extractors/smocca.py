"""賃貸スモッカ の詳細ページから写真・間取り図・諸元を取る。
2026-10-10 調査（別URLで再現確認済み）"""
import re
import html as _html
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup


# スライダー画像の alt は「<建物名>の<種別>」。種別で写真/間取り/地図を振り分ける
_PLAN_KINDS = ("間取り",)
_SKIP_KINDS = ("地図",)  # 地図画像は写真ではない


def _kind(alt: str) -> str:
    """alt 末尾の「の外観」「の間取り」「の地図」等から種別を返す"""
    m = re.search(r"の([^の]+)$", alt or "")
    return m.group(1) if m else ""


def _unwrap(u: str) -> str:
    """image.smocca.jp/filter/?width=720&...&url=<実URL> を実URLに開く。
    実測: filter に width=1280 を渡しても元画像(SUUMO _go.jpg は 308x315)を
    拡大するだけなので、url= の実URLをそのまま使う。
    リライフ系(cdn.relife-search.com)の実URLは 768x1024 と大きい。"""
    if not u:
        return ""
    u = _html.unescape(_html.unescape(u)).strip()
    if "image.smocca.jp/filter/" in u:
        q = parse_qs(urlparse(u).query)
        inner = (q.get("url") or [""])[0]
        if inner.startswith("http"):
            return inner
    return u


def _img_url(img, base_url: str) -> str:
    for attr in ("data-original", "data-src", "src"):
        u = img.get(attr) or ""
        if isinstance(u, list):
            u = u[0] if u else ""
        u = u.strip()
        if not u or u.startswith("data:") or "loading_" in u:
            continue  # lazy画像の src はローディングGIF
        return urljoin(base_url, _unwrap(u))
    return ""


def _cell_text(td) -> str:
    """td から「初期費用を知りたい」「中央区の賃貸を探す」等のボタンを除いた本文。
    会社名リンク(btn_link02)は文字を残す。br は改行にする(主要交通機関は br 区切り)。
    特徴タグは enable_tags だけ残す(disable_tags はグレー＝該当しない)。"""
    td = BeautifulSoup(str(td), "html.parser").find(["td", "th"])
    for a in td.find_all("a"):
        cls = " ".join(a.get("class", []))
        if "btn01" in cls or "btn_link01" in cls or "js_favorite" in cls:
            a.decompose()
        else:
            a.unwrap()
    for sp in td.select("span.disable_tags"):
        sp.decompose()
    for br in td.find_all("br"):
        br.replace_with("\n")
    txt = td.get_text(" ", strip=False)
    txt = re.sub(r"[ \t　]*\n[ \t　]*", "\n", txt)
    txt = re.sub(r"[^\S\n]+", " ", txt)  # \xa0 や全角空白の連続も1個の空白に
    txt = "\n".join(l.strip() for l in txt.split("\n") if l.strip())
    txt = re.sub(r"\s*※お問い合わせの際には.*$", "", txt, flags=re.S)
    return txt.strip()


def _table_pairs(table) -> dict:
    """th/td 交互の行(1行に th,td,th,td の2組もある)を {label: value} に"""
    out = {}
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        i = 0
        while i < len(cells) - 1:
            if cells[i].name == "th" and cells[i + 1].name == "td":
                label = cells[i].get_text(" ", strip=True)
                if label and label not in out:
                    out[label] = _cell_text(cells[i + 1])
                i += 2
            else:
                i += 1
    return out


def _parse_bukken_info(txt: str) -> dict:
    """取扱会社表の「物件に関する情報」は
    「物件の所在地 : X / 交通の利便 : Y / ... / 敷金 : 1ヶ月、保証金等 : －、 償却、敷引 : － / ...」
    という「 / 」区切りの一覧。保証金・償却・敷引・損害保険料はここにしか無い。"""
    out = {}
    body = txt.split("その他 :")[0]  # その他以降はPR文(/ を含む)
    body = body.replace("\n", " ").replace("償却、敷引", "償却・敷引")
    for part in re.split(r"\s/\s*", body):
        for kv in re.split(r"、\s*(?=[^、:：]+\s*[:：]\s)", part):
            m = re.match(r"\s*([^:：]+?)\s*[:：]\s*(.+?)\s*$", kv)
            if m:
                out[m.group(1)] = m.group(2)
    m = re.search(r"/\s*駐車場\s*[:：]\s*([^\n/]+)", txt)
    if m:
        out["駐車場"] = m.group(1).strip()
    return out


def extract(soup: BeautifulSoup, base_url: str) -> dict:
    photos, plan = [], None
    seen = set()

    # 1) 物件画像: メインスライダー div.slider_detail ul.slides li img.detail_thumb だけ見る。
    #    サムネ列(div.slider_thumb_detail)は同じ画像の重複。
    #    「空室状況」「よく似たおすすめ物件」の間取りサムネ(width=315)は別部屋/別物件。
    slider = soup.select_one("div.slider_detail")
    if slider is not None:
        for img in slider.select("ul.slides li img.detail_thumb"):
            u = _img_url(img, base_url)
            if not u or u in seen:
                continue
            seen.add(u)
            k = _kind(img.get("alt", ""))
            if k in _PLAN_KINDS:
                if plan is None:
                    plan = u
                continue
            if k in _SKIP_KINDS:
                continue
            photos.append(u)

    # フォールバック: スライダーが無いときは og:image
    if not photos and plan is None:
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            photos.append(urljoin(base_url, _unwrap(og["content"])))

    # 2) 諸元: table.table01.table_size_g (物件情報 + 契約条件の2表) と
    #    取扱会社表 table.table01.d_table_fixed.table_size_c (最初の1社のみ採用)
    specs = {}
    for t in soup.select("table.table01.table_size_g"):
        for k, v in _table_pairs(t).items():
            specs.setdefault(k, v)
    agent_tables = soup.select("table.table01.d_table_fixed.table_size_c")
    if agent_tables:
        for k, v in _table_pairs(agent_tables[0]).items():
            specs.setdefault(re.sub(r"\(\d+\)$", "", k), v)  # 取扱会社(1) -> 取扱会社
        specs["取扱会社数"] = str(len(agent_tables))

    # 3) 複合セルの分解(元のラベルも残す)
    m = re.match(r"\s*([\d.,]+万円)\s*(?:（\s*管理費等\s*([^）]+)）)?", specs.get("賃料/管理費等", ""))
    if m:
        specs["賃料"] = m.group(1)
        if m.group(2):
            specs["管理費等"] = m.group(2).strip()
    if "/" in specs.get("種別/構造", ""):
        a, b = specs["種別/構造"].split("/", 1)
        specs["種別"], specs["構造"] = a.strip(), b.strip()
    m = re.match(r"\s*([^/]*?)\s*/\s*(.+)$", specs.get("階数/部屋番号", ""))
    if m:
        specs["所在階"], specs["階建"] = m.group(1), m.group(2)
    # 更新料は独立ラベルが無く、備考に「更新料　新賃料1.00ヶ月分」のように書かれる。
    # 「(更新料無)」(保証会社の月額保証に更新料なし)を拾わないよう数字必須。
    m = re.search(r"更新料[\s　:：]*((?:新?賃料(?:等)?)?\s*[\d.,]+\s*(?:ヶ月分|ヶ月|か月分|カ月分|ヵ月分|万円|円))",
                  specs.get("備考", ""))
    if m:
        specs.setdefault("更新料", m.group(1).strip())
    # 保証金・償却/敷引・損害保険料は「物件に関する情報」にしか無い
    info = _parse_bukken_info(specs.get("物件に関する情報", ""))
    for src, dst in (("保証金等", "保証金"), ("償却・敷引", "償却・敷引"),
                     ("住宅総合保険等の損害保険料", "損害保険料"),
                     ("礼金等", "礼金(月数)"), ("敷金", "敷金(月数)")):
        if info.get(src):
            specs.setdefault(dst, info[src])

    # 4) 情報公開日(詳細ページ上部)
    d = soup.select_one(".bukken_provided_date")
    if d:
        m = re.search(r"情報公開日[:：]\s*([\d/]+)", d.get_text(" ", strip=True))
        if m:
            specs["情報公開日"] = m.group(1)

    return {"photos": photos, "plan": plan, "specs": specs}


# 動作確認(実測): 3件とも fetch(url, impersonate=True) で 200。
#   p0 photos=16 plan=..._co.jpg / p1 photos=17 / p2 photos=25 (リライフ系 cdn.relife-search.com)
# import sys; sys.path.insert(0, "/Users/sho/code/bukken-pool"); from watcher import fetch
# soup = BeautifulSoup(fetch(url, impersonate=True), "html.parser"); extract(soup, url)

