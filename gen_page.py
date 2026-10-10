"""プール全件を1枚のHTMLにする（毎朝の通知とは別。今ある在庫を全部見るため）"""
import html
import json
import os as _os
import re as _re
from datetime import datetime, timezone, timedelta

TYPE_LABEL = {"mansion": "マンション", "house": "戸建", "land": "土地", "rent": "賃貸"}
TYPE_ICON = {"mansion": "🏢", "house": "🏠", "land": "🏞", "rent": "🔑"}
CURRENT_YEAR = 2026


_FW = str.maketrans(
    "０１２３４５６７８９"
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz")


def _norm_name(name: str) -> str:
    """建物名の表記ゆれを吸収する。
    実測で同じ建物が
      「戸越Ｂ．Ｉ．Ｇ．　ＲＥＳＩＤＥＮＣＥ」「戸越B.I.G　RESIDENCE」
      「戸越BIG RESIDENCE」「戸越Ｂ．ＩＧ．　ＲＥＳＩＤＥＮＣＥ」
    と4通りに割れていた。記号・空白・かっこ書きを落として突き合わせる。
    """
    if not name:
        return ""
    t = name.translate(_FW)
    t = _re.sub(r"[（(\[【].*?[）)\]】]", "", t)       # かっこ書き(読み仮名など)
    t = _re.sub(r"(新築|中古|分譲|賃貸|マンション情報)", "", t)
    t = _re.sub(r"[^0-9A-Za-zぁ-んァ-ヴ一-龥]", "", t)   # 記号・空白を全部落とす
    return t.lower()


def _norm_addr(a: str) -> str:
    if not a:
        return ""
    t = a.translate(_FW).replace("東京都", "")
    t = _re.sub(r"[\s　]", "", t)
    t = _re.sub(r"[‐‑‒–—―ーｰ−\-]", "-", t)
    t = t.replace("丁目", "-").replace("番地", "-").replace("番", "-").replace("号", "")
    return _re.sub(r"-+", "-", t).strip("-")[:14]


def _is_building_name(n: str) -> bool:
    """住所や説明文が名前欄に入っているケースを弾く。
    実測: スマイティは「都営浅草線戸越駅まで徒歩5分」が名前に入る。
    """
    if len(n) < 3:
        return False
    if _re.search(r"(徒歩\d|駅まで|階建|万円|築\d+年)", n):
        return False
    if _re.match(r"^(東京都)?[^\d]{2,6}[区市][^\d]{0,8}\d", n):
        return False          # 「目黒区下目黒6」のような住所そのもの
    return True


def group_items(items):
    """同じ物件の掲載をまとめる。名前と「住所＋面積」の2つのキーで
    ゆるく繋ぐ（片方しか取れないポータルがあるため）。
    戻り値: [(代表, [同じ物件の全掲載])]
    """
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    keys_of = []
    for i, it in enumerate(items):
        me = ("I", i)
        parent.setdefault(me, me)
        ks = []
        nn = _norm_name(it.get("name") or "")
        if _is_building_name(nn):
            ks.append(("N", it.get("station"), it.get("type"), nn))
        na = _norm_addr(it.get("addr") or "")
        ar = it.get("area")
        if na and _re.search(r"\d", na) and ar:
            ks.append(("A", it.get("station"), it.get("type"), na, round(ar, 1)))
        for k in ks:
            parent.setdefault(k, k)
            union(me, k)
        keys_of.append(ks)

    buckets = {}
    for i, it in enumerate(items):
        root = find(("I", i)) if keys_of[i] else ("I", i)
        buckets.setdefault(root, []).append(it)
    return list(buckets.values())


def _img_rank(it):
    """代表に出す写真の良さ。間取り図・画像なしを後ろに回す。
    URLの文字列では判別できないので、watcher 側が画像そのものを見て
    付けた img_is_plan を最優先で使う。
    """
    u = it.get("img") or ""
    if not u.startswith("http"):
        return 9
    if it.get("img_is_plan"):
        return 5
    if _re.search(r"(madori|間取|floor_?plan|zumen|/fp/|_fp[._])", u, _re.I):
        return 5
    return 0


# 代表カードのリンク先に使うソースの優先順。
# goo住宅は他社の掲載を中継しているだけで、gooが弾くとリンクを開けない
# （実測: goo詳細は403、SUUMOは200）。開けるサイトを優先し、gooは最後にする
_SRC_RANK = {"SUUMO": 0, "SUUMO賃貸": 0, "三井のリハウス": 1, "ノムコム": 1,
             "リバブル": 1, "リバブル賃貸": 1, "HOMES": 2, "HOMES賃貸": 2,
             "CHINTAI": 3, "ハウスコム": 3, "スマイティ": 4,
             "スマイティ賃貸": 4, "賃貸スモッカ": 4, "カウカモ": 4,
             "ニフティ不動産": 5, "goo住宅": 9}


def pick_rep(group):
    """写真が分かりやすいものを代表にする。写真の質が同じなら
    情報が埋まっているもの→安いものの順。"""
    def key(it):
        # 写真の分かりやすさが最優先。次に建物名が実名で入っているもの
        # （スマイティは「◯◯駅まで徒歩5分」、SUUMOは「品川区大井２ 賃貸」
        #  のように名前欄が説明文・住所になることがある）
        return (_SRC_RANK.get(it.get("source"), 6),
                _img_rank(it),
                0 if _is_building_name(_norm_name(it.get("name") or "")) else 1,
                0 if it.get("floor") else 1,
                0 if it.get("parking") else 1,
                it.get("price") if it.get("price") is not None else 9e9)
    return sorted(group, key=key)[0]

# クリックしても開けないサイト。ここへのリンクしか無い物件はページに出さない。
# 実測(2026-09-16): house.goo.ne.jp と www.chintai.net が403を返す。
# 検索リンクで代用したが「googleじゃ意味ねえだろ」と却下されたので、
# 物件ページに繋げないものは載せない方針にした。
# 環境変数 UNOPENABLE_HOSTS で指定したホストの掲載はページに出さない。
# 2026-09-16に goo/CHINTAI が403を返した時だけ一時的に使った。
# 復旧を実測(2026-09-17: 一覧・詳細とも200)したので既定は空にする。
# 恒久的に塞がったサイトが出たら、そのホストをここに足す。
UNOPENABLE_HOSTS = tuple(
    h.strip() for h in (_os.environ.get("UNOPENABLE_HOSTS") or "").split(",")
    if h.strip())


def is_openable(it):
    u = it.get("url") or ""
    return bool(u) and not any(h in u for h in UNOPENABLE_HOSTS)


# 全角英数の物件名（Ｒｅｓｉｄｅｎｃｅ　Ｃａｒｉｔａｓ など）はスマホで
# 文字が間延びして2行に割れる。半角に直して読めるようにする。
_FW2HW = str.maketrans(
    "０１２３４５６７８９"
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
    "　（）［］／－",
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    " ()[]/-")


def tidy_name(n: str) -> str:
    if not n:
        return n
    t = n.translate(_FW2HW)
    return _re.sub(r"\s{2,}", " ", t).strip()

def fmt_price(it):
    p = it.get("price")
    if p is None:
        return "価格記載なし"
    if it.get("type") == "rent":
        return f"{p}万円/月<span class='sub'>(管理費込)</span>"
    if p >= 10000:
        oku, rest = p // 10000, p % 10000
        return f"{oku}億{rest:,}万円" if rest else f"{oku}億円"
    return f"{p:,}万円"


def fmt_walk(it):
    ws = it.get("walks")
    if ws:
        return " / ".join(f"{html.escape(n)} 徒歩{m}分" for n, m in ws[:4])
    if it.get("walk"):
        return f"{html.escape(it['station'])} 徒歩{it['walk']}分"
    return "徒歩記載なし"


def fmt_parking(it):
    pk = it.get("parking")
    label = {"有": "🚗駐車場あり", "近隣": "🚗駐車場あり(近隣)",
             "空無": "🚗駐車場あり(空きなし)", "無": "駐車場なし",
             "—": "駐車場なし(土地)"}.get(pk, "駐車場 未確認")
    pp = it.get("parking_price")
    if pp and pk in ("有", "近隣", "空無"):
        label += f" {html.escape(pp)}"
    return label




# ---------------------------------------------------------------------------
# 見開き（詳細パネル）付きページ。2026-10-10 sho指示:
# 「一回一回クリックしてそのページに飛ぶのはめんどくさい。押したら見開きタブ
#  みたいに情報を見れるように。とにかく見やすく」
# 一覧カードをタップすると、写真ギャラリー・費用・諸元・全駅徒歩・地図・
# 同じ建物の掲載を1画面で見られるパネルが開く。元ページへはパネル内のボタンで飛ぶ。
# ゲート(verify_page.py)が見る要素（.card の data-*、a.lnk、.thumb img、
# .price、駅名 徒歩N分、.meta pk、駅チップ）は形を変えずに残している。
# ---------------------------------------------------------------------------
import hashlib

# 掲載側の駅名（JR蒲田は掲載では「蒲田」）
_ALIAS = {"JR蒲田": "蒲田"}


def _stn_match(st):
    return _ALIAS.get(st, st)


def pid_of(it, n=8):
    return hashlib.sha1(it["id"].encode()).hexdigest()[:n]


def fmt_price_text(it):
    """fmt_price のタグ無し版（JSON用）"""
    p = it.get("price")
    if p is None:
        return "価格記載なし"
    if it.get("type") == "rent":
        return f"{p}万円/月"
    if p >= 10000:
        oku, rest = p // 10000, p % 10000
        return f"{oku}億{rest:,}万円" if rest else f"{oku}億円"
    return f"{p:,}万円"


def fmt_parking_text(it):
    pk = it.get("parking")
    label = {"有": "🚗駐車場あり", "近隣": "🚗駐車場あり(近隣)",
             "空無": "🚗駐車場あり(空きなし)", "無": "駐車場なし",
             "—": "駐車場なし(土地)"}.get(pk, "駐車場 未確認")
    pp = it.get("parking_price")
    if pp and pk in ("有", "近隣", "空無"):
        label += f" {pp}"
    return label


def age_text(it):
    if it.get("type") == "land":
        return "更地/土地"
    b = it.get("built")
    if not b:
        return "築年記載なし"
    y = CURRENT_YEAR - b
    return "新築" if y <= 0 else f"築{y}年"


def walks_all(it):
    """全駅の徒歩。監視駅を先頭（太字フラグ付き）、他は近い順。"""
    st = it["station"]
    mt = _stn_match(st)
    ws = [[n, m] for n, m in (it.get("walks") or []) if n and m is not None]
    if it.get("walk") is not None and not any(n == mt for n, _ in ws):
        ws.insert(0, [mt, it["walk"]])
    ws.sort(key=lambda x: (0 if x[0] == mt else 1, x[1]))
    out, seen = [], set()
    for n, m in ws:
        if n in seen:
            continue
        seen.add(n)
        out.append([st if n == mt else n, m, n == mt])
    return out


def range_tag(it):
    p = it.get("price")
    if p is None:
        return ""
    if it.get("type") == "rent":
        return "r20" if p <= 20 else ""
    return "b6" if p <= 6000 else ""


# ---- 諸元: 表示順と表示名。サイトごとにラベルが違うので寄せる ----
SPEC_ORDER = [
    ("向き", "向き"), ("主要採光面", "向き"), ("バルコニー方向", "向き"), ("方位", "向き"),
    ("構造", "構造"), ("建物構造", "構造"), ("種別/構造", "構造"), ("構造・階建て", "構造"),
    ("構造・階建", "構造"), ("所在階/構造・階建", "所在階/構造"),
    ("所在階", "所在階"), ("所在階/階数", "所在階/階数"), ("所在階数", "所在階"),
    ("階建", "階建"), ("階建/階", "階建"),
    ("総戸数", "総戸数"), ("総区画数", "総区画数"),
    ("入居時期", "入居"), ("入居可能時期", "入居"), ("入居日", "入居"), ("入居", "入居"),
    ("引渡可能時期", "引渡"), ("引渡時期", "引渡"), ("引き渡し時期", "引渡"),
    ("契約期間", "契約期間"), ("更新料", "更新料"),
    ("保証金", "保証金"), ("敷引・償却", "敷引・償却"), ("保証金/敷引・償却", "保証金/敷引"),
    ("保証金/敷引(償却)", "保証金/敷引"), ("損害保険", "保険"), ("保険", "保険"),
    ("保証会社", "保証会社"),
    ("土地面積", "土地面積"), ("建物面積", "建物面積"),
    ("バルコニー面積", "バルコニー"), ("バルコニー", "バルコニー"), ("その他面積", "その他面積"),
    ("用途地域", "用途地域"), ("建ぺい率・容積率", "建ぺい率/容積率"),
    ("建ぺい率", "建ぺい率"), ("容積率", "容積率"), ("私道負担・道路", "道路"),
    ("接道状況", "道路"), ("建築条件", "建築条件"), ("地目", "地目"), ("現況", "現況"),
    ("敷地の権利形態", "権利"), ("土地の権利形態", "権利"), ("土地権利", "権利"),
    ("権利形態", "権利"), ("管理形態", "管理"), ("管理方式", "管理"), ("管理会社", "管理会社"),
    ("リフォーム", "リフォーム"), ("取引態様", "取引態様"),
    ("設備・特徴", "設備"), ("設備", "設備"), ("設備・条件", "設備"), ("部屋の特徴・設備", "設備"),
    ("条件", "条件"), ("その他制限事項", "制限事項"), ("備考", "備考"),
    ("その他概要・特記事項", "備考"), ("その他", "備考"),
]
SPEC_DROP = ("管理費", "共益費", "修繕積立", "駐車場", "敷金", "礼金", "賃料", "家賃", "価格",
             "専有面積", "間取", "築年", "完成時期", "交通", "最寄", "駅徒歩", "所在地", "住所",
             "問合", "問い合", "会社", "物件番号", "情報公開", "情報提供", "次回更新", "更新日",
             "掲載日", "画像", "地図", "取扱", "免許", "電話", "TEL", "店舗", "タイトル", "物件名",
             "建物名", "部屋番号", "担当", "取引条件有効", "販売", "スケジュール", "イベント",
             "支払", "ポイント", "キャンペーン", "おすすめ", "関連", "ガイド", "周辺")
SPEC_MAX = 300


def _norm_label(s):
    return _re.sub(r"[\s　:：]", "", (s or "").translate(_FW)).replace("（", "(").replace("）", ")")


def _clean_val(v):
    v = _re.sub(r"\s+", " ", (v or "")).strip()
    if v in ("", "-", "－", "―", "—", "ー"):
        return ""
    return v if len(v) <= SPEC_MAX else v[:SPEC_MAX] + "…"


def _spec_any(raw, *prefixes):
    """ラベルが prefixes のどれかで始まる最初の値"""
    for k, v in (raw or {}).items():
        nk = _norm_label(k)
        if any(nk.startswith(p) for p in prefixes):
            cv = _clean_val(v)
            if cv:
                return cv
    return ""


def build_specs(raw):
    norm = {}
    for k, v in (raw or {}).items():
        nk = _norm_label(k)
        if nk and nk not in norm:
            norm[nk] = v
    out, used_disp, used_key = [], set(), set()
    for key, disp in SPEC_ORDER:
        if key in norm and disp not in used_disp:
            v = _clean_val(norm[key])
            if v:
                out.append([disp, v])
                used_disp.add(disp)
                used_key.add(key)
    for key, v in norm.items():
        if key in used_key or any(d in key for d in SPEC_DROP):
            continue
        v = _clean_val(v)
        if v and len(out) < 40:
            out.append([key[:14], v])
    return out


def _man(x):
    return f"{x:g}万円" if isinstance(x, (int, float)) else (str(x) if x else "記載なし")


def _yen(s):
    """'1万2,000円' / '12,000円' / '1.2万円' → 円。読めなければ None"""
    if not s:
        return None
    t = s.translate(_FW).replace(",", "")
    m = _re.search(r"(?:(\d+(?:\.\d+)?)万)?(\d+)?円", t)
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(float(m.group(1) or 0) * 10000 + int(m.group(2) or 0))


def cost_rows(it, raw):
    rows = []
    p = it.get("price")
    if it.get("type") == "rent":
        kanri, rent = it.get("kanri"), it.get("rent")
        if rent is None and p is not None and isinstance(kanri, (int, float)):
            rent = round(p - kanri, 2)
        if rent is not None:
            rows.append(["賃料", _man(rent)])
        if kanri not in (None, ""):
            rows.append(["管理費", _man(kanri)])
        else:
            k = _spec_any(raw, "管理費", "共益費")
            rows.append(["管理費", k or "記載なし"])
        if p is not None:
            rows.append(["合計(管理費込)", f"{p}万円/月"])
        sr = it.get("shikirei") or ""
        if not sr:
            sk, rk = _spec_any(raw, "敷金"), _spec_any(raw, "礼金")
            sr = " / ".join(x for x in (f"敷金 {sk}" if sk else "", f"礼金 {rk}" if rk else "") if x)
        rows.append(["敷金・礼金", sr or "記載なし"])
        for pre, disp in (("更新料", "更新料"), ("保証金", "保証金"), ("敷引", "敷引・償却"),
                          ("損害保険", "保険"), ("保険", "保険"), ("保証会社", "保証会社")):
            v = _spec_any(raw, pre)
            if v and not any(r[0] == disp for r in rows):
                rows.append([disp, v])
    else:
        rows.append(["価格", fmt_price_text(it)])
        if p and it.get("area"):
            rows.append(["㎡単価", f"{p / it['area']:.1f}万円/㎡"])
        if it.get("type") == "mansion":
            k, s = _spec_any(raw, "管理費"), _spec_any(raw, "修繕積立金")
            rows.append(["管理費", k or "記載なし"])
            rows.append(["修繕積立金", s or "記載なし"])
            ky, sy = _yen(k), _yen(s)
            if ky is not None and sy is not None:
                rows.append(["月額合計", f"{ky + sy:,}円/月"])
        o = _spec_any(raw, "諸費用", "その他費用")
        if o:
            rows.append(["その他費用", o])
    if it.get("parking") in ("有", "近隣", "空無"):
        rows.append(["駐車場料金", it.get("parking_price") or "料金記載なし"])
    return rows


def listing_text(it):
    d, c = it.get("_days"), it.get("_cuts") or 0
    bits = ["本日掲載" if not d else f"掲載{d}日目"]
    if c:
        bits.append(f"値下げ{c}回")
    if (d or 0) >= 90:
        bits.append("90日以上")
    return " ・ ".join(bits)


def _floor_txt(x):
    return "階なし(土地)" if x.get("type") == "land" else (x.get("floor") or "階記載なし")


def _sib_ord(x):
    m = _re.search(r"(\d+)", x.get("floor") or "")
    return (int(m.group(1)) if m else 999,
            x.get("price") if x.get("price") is not None else 9e9)


def _photos_of(it):
    ps = [u for u in (it.get("photos") or []) if isinstance(u, str) and u.startswith("http")]
    img = it.get("img") or ""
    # 一覧のサムネが外観で、詳細の写真に無いなら先頭に足す（必ず1枚は出す）
    _bad = _re.compile(r"(brand\.png|/logo|logo[_.-]|noimage|no_image|nophoto|no_photo|spacer)", _re.I)
    ps = [u for u in ps if not _bad.search(u)]
    if (img.startswith("http") and not it.get("img_is_plan") and img not in ps
            and not _bad.search(img)):
        ps.insert(0, img)
    plan = it.get("plan") or ""
    if plan:
        ps = [u for u in ps if u != plan]
    if not ps and img.startswith("http") and not _bad.search(img):
        ps = [img]
    return ps[:20], plan


def item_json(it, pid, allx):
    raw = it.get("specs") or {}
    photos, plan = _photos_of(it)
    return {
        "id": pid, "src_id": it["id"], "url": it["url"], "source": it.get("source", ""),
        "station": it["station"], "type": it.get("type", ""),
        "type_label": TYPE_LABEL.get(it.get("type"), ""),
        "type_icon": TYPE_ICON.get(it.get("type"), ""),
        "name": tidy_name(it.get("name") or ""), "addr": it.get("addr") or "",
        "price": it.get("price"), "price_s": fmt_price_text(it),
        "area": it.get("area"),
        "area_s": f"{it['area']}㎡" if it.get("area") else "面積記載なし",
        "layout": it.get("layout") or "", "floor": _floor_txt(it),
        "built": it.get("built"), "age": age_text(it),
        "walk": it.get("walk"), "walks": walks_all(it),
        "parking": it.get("parking"), "pk_label": fmt_parking_text(it),
        "photos": photos, "plan": plan,
        "cost": cost_rows(it, raw), "specs": build_specs(raw),
        "deal": bool(it.get("_deal")), "price_down": bool(it.get("_price_down")),
        "price_note": it.get("_price_note") or "",
        "days": it.get("_days") or 0,
        "listing_s": listing_text(it), "hist_note": it.get("_hist_note") or "",
        "dup_note": it.get("_dup_note") or "",
        "sibs": [{"url": x["url"], "source": x.get("source", ""),
                  "price_s": fmt_price_text(x), "floor": _floor_txt(x),
                  "area": x.get("area"),
                  "shikirei": (x.get("shikirei") or "") if x.get("type") == "rent" else "",
                  "img": x.get("img") if (x.get("img") or "").startswith("http") else ""}
                 for x in sorted(allx, key=_sib_ord)],
    }


def dump_json(d):
    return (json.dumps(d, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


CSS = r"""
:root{--bg:#faf9f7;--fg:#1c1b19;--sub:#6b6862;--line:#e6e3dd;--card:#fff;
  --accent:#1a5d3a;--buy:#1a5d3a;--rent:#b4560a;--gold:#c8912a;--down:#c0392b;--newc:#1f6feb;
  --r:12px;--tap:44px;--thw:132px;--thh:112px;--pw:480px;
  --sat:env(safe-area-inset-top);--sab:env(safe-area-inset-bottom)}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--fg:#f0eee9;--sub:#a3a099;--line:#2c2b28;
  --card:#1c1b19;--accent:#4ea87a;--buy:#4ea87a;--rent:#e59a4d;--gold:#d8b74a;--down:#ff7a6b;--newc:#6ea8ff}}
*{box-sizing:border-box}
html,body{overflow-x:hidden}
body{margin:0;background:var(--bg);color:var(--fg);-webkit-text-size-adjust:100%;
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif}
[hidden]{display:none!important}
body.lock{position:fixed;left:0;right:0;overflow:hidden}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;padding:0}
a{color:inherit}
#hd{position:sticky;top:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--line);
  padding-top:var(--sat)}
.row1{display:flex;align-items:center;gap:8px;min-height:48px;padding:0 12px}
h1{margin:0;font-size:16px;flex:none}
.count{flex:1;min-width:0;color:var(--sub);font-size:12px;line-height:1.35}
.count b{color:var(--fg);font-size:14px}
.ts{display:block;opacity:.75;font-size:11px}
#sortbtn{flex:none;min-height:40px;padding:0 12px;border:1px solid var(--line);border-radius:999px;
  background:var(--card);font-size:13px;white-space:nowrap}
.filters{display:flex;gap:6px;padding:4px 12px;overflow-x:auto;scrollbar-width:none;
  -webkit-overflow-scrolling:touch;overscroll-behavior-x:contain}
.filters::-webkit-scrollbar{display:none}
.chip{flex:none;min-height:36px;padding:0 13px;border:1px solid var(--line);border-radius:999px;
  background:var(--card);font-size:13px;white-space:nowrap}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.vr{flex:none;width:1px;background:var(--line);margin:6px 2px}
#active-bar{display:flex;align-items:center;gap:8px;padding:2px 12px 6px;font-size:12px;color:var(--sub)}
#active-txt{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#clear{flex:none;min-height:32px;padding:0 10px;border-radius:8px;background:var(--line);font-size:12px}
main{display:grid;grid-template-columns:1fr;gap:10px;padding:10px 12px max(16px,var(--sab))}
.sep{font-size:12px;font-weight:700;color:var(--sub);padding:6px 2px 0}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--buy);
  border-radius:var(--r);overflow:hidden}
.card[data-type="rent"]{border-left-color:var(--rent)}
.card[data-deal="1"]{box-shadow:inset 0 0 0 2px var(--gold)}
.lnk{display:grid;grid-template-columns:var(--thw) 1fr;gap:10px;min-height:var(--thh);
  text-decoration:none;color:inherit;-webkit-tap-highlight-color:transparent}
.lnk:active{background:var(--bg)}
.thumb{position:relative;width:var(--thw);height:100%;min-height:var(--thh);background:var(--line)}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.noimg{display:flex;align-items:center;justify-content:center;height:100%;min-height:var(--thh);
  font-size:11px;color:var(--sub)}
.thumb .n{position:absolute;right:4px;bottom:4px;padding:1px 7px;border-radius:999px;
  background:rgba(0,0,0,.6);color:#fff;font-size:11px;line-height:1.5}
.body{padding:8px 10px 8px 0;min-width:0;display:flex;flex-direction:column;gap:1px;justify-content:center}
.tag{display:flex;gap:6px;align-items:center;font-size:11.5px;color:var(--sub);white-space:nowrap;overflow:hidden}
.tp{font-weight:700;color:var(--buy)}
.card[data-type="rent"] .tp{color:var(--rent)}
.stn{font-weight:700;color:var(--fg)}
.deal{background:var(--gold);color:#1b1a18;font-size:10.5px;font-weight:700;border-radius:4px;padding:0 5px}
.dn{color:var(--down);font-weight:700}
.new{color:var(--newc);font-weight:700}
.price{font-size:21px;font-weight:800;line-height:1.25;font-variant-numeric:tabular-nums;letter-spacing:-.01em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.price .sub{font-size:11px;font-weight:400;color:var(--sub);margin-left:3px}
.cmp{display:flex;font-size:13.5px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden}
.cmp span+span::before{content:"·";color:var(--sub);margin:0 6px;font-weight:400}
.meta{font-size:12px;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta.row{display:flex;gap:10px}
.walk{color:var(--fg);flex:none}
.more-st{color:var(--sub);margin-left:4px}
.row .pk{min-width:0;overflow:hidden;text-overflow:ellipsis}
.card[data-parking="1"] .pk{color:var(--fg)}
.name{font-size:12px;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.name .src{opacity:.75;margin-left:6px}
.rooms{color:var(--accent);font-weight:700;margin-left:6px}
.empty{padding:40px 16px;text-align:center;color:var(--sub)}
#clear2{display:block;margin:12px auto 0;min-height:var(--tap);padding:0 18px;border:1px solid var(--line);border-radius:10px}
.rej{margin:18px 0;padding:10px 14px;background:var(--card);border:1px solid var(--line);
  border-radius:10px;font-size:13px;color:var(--sub)}
.rej summary{cursor:pointer;font-weight:600;color:var(--fg)}
.rej ul{margin:8px 0 4px;padding-left:20px}.rej li{margin:2px 0}.rej p{margin:6px 0 0;opacity:.8;font-size:12px}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:20}
.panel{position:fixed;left:0;right:0;bottom:0;height:92vh;height:92dvh;z-index:21;background:var(--bg);
  border-radius:16px 16px 0 0;box-shadow:0 -8px 30px rgba(0,0,0,.25);
  display:grid;grid-template-rows:var(--tap) 1fr auto;
  transform:translateY(100%);transition:transform .24s cubic-bezier(.2,.8,.2,1);will-change:transform}
.panel.open{transform:none}
@media(prefers-reduced-motion:reduce){.panel{transition:none}}
.ph{position:relative;display:grid;place-items:center;touch-action:none}
.ph .grab{width:40px;height:5px;border-radius:3px;background:var(--line)}
#ppos{position:absolute;left:14px;top:0;line-height:var(--tap);font-size:12px;color:var(--sub);
  font-variant-numeric:tabular-nums}
.ph .x{position:absolute;right:4px;top:0;width:var(--tap);height:var(--tap);font-size:26px;
  display:grid;place-items:center;color:var(--sub)}
.pb{overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;padding-bottom:12px;min-height:0}
.gal{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;height:270px;background:#000;
  scrollbar-width:none;overscroll-behavior-x:contain}
.gal::-webkit-scrollbar{display:none}
.gal .sl{flex:none;width:100%;height:100%;scroll-snap-align:start;display:block}
.gal img{width:100%;height:100%;object-fit:contain;display:block}
.gal .plan{background:#fff}
.gal.none{display:grid;place-items:center;height:120px;color:var(--sub);background:var(--line)}
.gbar{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 16px;font-size:12px;color:var(--sub)}
.toplan{min-height:34px;padding:0 12px;border:1px solid var(--line);border-radius:8px;font-size:12.5px;color:var(--fg)}
.phd{padding:4px 16px 0}
.ptag{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;color:var(--sub)}
.ptag .tp.rent{color:var(--rent)}
.pname{margin:4px 0 2px;font-size:17px;font-weight:700;overflow-wrap:anywhere;line-height:1.4}
.pprice{font-size:28px;font-weight:800;line-height:1.2;font-variant-numeric:tabular-nums}
.pprice small{font-size:12px;font-weight:400;color:var(--sub);margin-left:6px}
.pnote{color:var(--down);font-weight:700;font-size:13px;margin-top:2px}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:12px 16px}
.tiles div{background:var(--card);padding:10px 4px;text-align:center;min-width:0}
.tiles b{display:block;font-size:16px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tiles small{color:var(--sub);font-size:11px}
.cta{display:grid;place-items:center;min-height:48px;margin:0 16px 8px;border-radius:10px;
  background:var(--fg);color:var(--bg);font-weight:700;text-decoration:none}
.cta.end{margin-top:14px}
.sec{padding:12px 16px 0}
.sec h3{margin:0 0 6px;font-size:12px;color:var(--sub);font-weight:700;letter-spacing:.04em}
.sec p{margin:0 0 6px;overflow-wrap:anywhere}
.kv{display:grid;grid-template-columns:7.5em 1fr;gap:5px 12px;margin:0;font-size:14px}
.kv dt{color:var(--sub)}.kv dd{margin:0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.pk.unk{color:var(--sub)}
.walks{display:flex;flex-wrap:wrap;gap:6px 14px}
.wk.tgt{font-weight:700}.wk.tgt b{color:var(--accent)}
.btn{display:inline-grid;place-items:center;min-height:var(--tap);padding:0 14px;
  border:1px solid var(--line);border-radius:10px;text-decoration:none}
.sibs{display:flex;flex-direction:column;gap:6px}
.sib{display:grid;grid-template-columns:56px 1fr 24px;gap:10px;align-items:center;min-height:var(--tap);
  padding:6px 8px;border:1px solid var(--line);border-radius:10px;text-decoration:none}
.sib img,.sibno{width:56px;height:42px;object-fit:cover;border-radius:6px;background:var(--line);display:block}
.sibb{display:flex;flex-direction:column;min-width:0;line-height:1.35}
.sibb b{font-size:14px}.sibb span{font-size:12px;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.arw{color:var(--sub);text-align:center}
.pf{display:grid;grid-template-columns:1fr 2fr 1fr;gap:8px;padding:8px 12px max(8px,var(--sab));
  border-top:1px solid var(--line);background:var(--bg)}
.pf>*{min-height:var(--tap);border-radius:10px;display:grid;place-items:center;border:1px solid var(--line);
  text-decoration:none;font-weight:700}
.pf .pri{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.pf button:disabled{opacity:.35}
.sheet{position:fixed;left:0;right:0;bottom:0;z-index:22;background:var(--bg);
  border-radius:16px 16px 0 0;padding:8px 12px max(12px,var(--sab));box-shadow:0 -8px 30px rgba(0,0,0,.25)}
.sheet button{display:flex;width:100%;min-height:var(--tap);align-items:center;padding:0 12px;border-radius:10px;font-size:15px}
.sheet button.on{background:var(--line);font-weight:700}
@media(min-width:640px){main{grid-template-columns:repeat(auto-fill,minmax(380px,1fr));padding:14px 16px}}
@media(min-width:900px){
  .filters{flex-wrap:wrap;overflow:visible}
  body.popen #hd,body.popen main{margin-right:var(--pw)}
  body.popen #scrim{display:none!important}
  .panel{left:auto;top:0;width:var(--pw);height:100vh;border-radius:0;box-shadow:none;
    border-left:1px solid var(--line);transform:translateX(100%)}
  .panel.open{transform:none}
  .ph{touch-action:auto}
  .sheet{left:auto;right:16px;bottom:auto;top:calc(var(--sat) + 52px);width:260px;
    border-radius:12px;border:1px solid var(--line);box-shadow:0 8px 30px rgba(0,0,0,.2)}
}
"""

JS = r"""
(()=>{
'use strict';
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let DATA={};
try{DATA=JSON.parse($('#data').textContent);}catch(e){return;}   // 壊れていたら普通のリンクのまま
const grid=$('#grid'), emptyEl=$('#empty'), shownEl=$('#shown'), bdEl=$('#bd');
const cards=$$('.card',grid);
const panel=$('#panel'), pbody=$('#pbody'), scrim=$('#scrim'), sortSheet=$('#sortsheet'), sortBtn=$('#sortbtn');
const state={active:{}, sort:'rec', cur:null, savedY:0};
const isDesk=()=>matchMedia('(min-width:900px)').matches;
const hashId=()=>{const m=location.hash.match(/^#p=([A-Za-z0-9]+)$/);return m?m[1]:null;};

const num=(el,k,d=0)=>{const v=parseFloat(el.dataset[k]);return Number.isNaN(v)?d:v;};
const blk=el=>el.dataset.type==='rent'?1:0;
const CMP={
  rec:(a,b)=>num(a,'ord')-num(b,'ord'),
  price_asc:(a,b)=>blk(a)-blk(b)||num(a,'price',9e9)-num(b,'price',9e9),
  price_desc:(a,b)=>blk(a)-blk(b)||num(b,'price')-num(a,'price'),
  area:(a,b)=>num(b,'area')-num(a,'area'),
  walk:(a,b)=>num(a,'walk',99)-num(b,'walk',99)||num(a,'ord')-num(b,'ord'),
  new:(a,b)=>num(a,'days',9e9)-num(b,'days',9e9)||num(a,'ord')-num(b,'ord'),
};
const SORT_LABEL={rec:'おすすめ',price_asc:'安い順',price_desc:'高い順',area:'広い順',walk:'駅近順',new:'新着順'};

function apply(){
  const ent=Object.entries(state.active);
  const vis=cards.filter(el=>ent.every(([k,v])=>el.dataset[k]===v));
  vis.sort(CMP[state.sort]);
  const visSet=new Set(vis);
  $$('.sep',grid).forEach(s=>s.remove());
  const frag=document.createDocumentFragment();
  let last=-1;
  for(const el of vis){
    if(state.sort.startsWith('price')){
      const b=blk(el);
      if(b!==last){last=b;const s=document.createElement('div');s.className='sep';s.textContent=b?'賃貸':'売買';frag.append(s);}
    }
    el.hidden=false; frag.append(el);
  }
  for(const el of cards) if(!visSet.has(el)){el.hidden=true;frag.append(el);}
  grid.insertBefore(frag,emptyEl);
  const rent=vis.filter(el=>el.dataset.type==='rent').length;
  shownEl.textContent=vis.length;
  bdEl.textContent=vis.length?`売買${vis.length-rent} / 賃貸${rent}`:'';
  emptyEl.hidden=vis.length>0;
  const on=$$('.chip.on');
  $('#active-bar').hidden=!on.length;
  $('#active-txt').textContent=on.map(c=>c.textContent.trim()).join('・');
  updateNav();
}
function clearAll(){state.active={};$$('.chip.on').forEach(c=>c.classList.remove('on'));apply();}
$$('.chip[data-f]').forEach(c=>c.addEventListener('click',()=>{
  const f=c.dataset.f, v=c.dataset.v;
  if(state.active[f]===v){delete state.active[f];c.classList.remove('on');}
  else{
    $$(`.chip[data-f="${f}"]`).forEach(o=>o.classList.remove('on'));
    state.active[f]=v; c.classList.add('on');
  }
  apply();
  window.scrollTo(0,0);
}));
$('#clear').onclick=clearAll; $('#clear2').onclick=clearAll;

function showSort(){sortSheet.hidden=false;if(!isDesk())scrim.hidden=false;}
function hideSort(){sortSheet.hidden=true;if(!state.cur)scrim.hidden=true;}
sortBtn.onclick=e=>{e.stopPropagation();sortSheet.hidden?showSort():hideSort();};
$$('button[data-sort]',sortSheet).forEach(b=>b.onclick=()=>{
  state.sort=b.dataset.sort;
  $$('button[data-sort]',sortSheet).forEach(x=>x.classList.toggle('on',x===b));
  sortBtn.textContent='⇅ '+SORT_LABEL[state.sort];
  hideSort(); apply(); window.scrollTo(0,0);
});
document.addEventListener('click',e=>{if(!sortSheet.hidden&&!sortSheet.contains(e.target)&&e.target!==sortBtn)hideSort();});

grid.addEventListener('click',e=>{
  const a=e.target.closest('a.lnk'); if(!a) return;
  if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||e.button!==0) return;
  const card=a.closest('.card'); if(!card||!DATA[card.dataset.id]) return;
  e.preventDefault(); openPanel(card.dataset.id);
});

const visibleIds=()=>$$('.card:not([hidden])',grid).map(el=>el.dataset.id);

function openPanel(id,{replace=false}={}){
  const d=DATA[id]; if(!d) return;
  const was=state.cur; state.cur=id;
  render(d);
  if(!was){
    state.savedY=window.scrollY;
    panel.hidden=false; sortSheet.hidden=true;
    document.body.classList.add('popen');
    if(!isDesk()){scrim.hidden=false;document.body.style.top=(-state.savedY)+'px';document.body.classList.add('lock');}
    requestAnimationFrame(()=>requestAnimationFrame(()=>panel.classList.add('open')));
  }
  const url='#p='+id;
  if(replace||was) history.replaceState({p:id},'',url); else history.pushState({p:id},'',url);
  pbody.scrollTop=0;
  updateNav(); preloadNext(id);
}
function closePanel(){
  if(!state.cur) return;
  const id=state.cur; state.cur=null;
  panel.classList.remove('open'); scrim.hidden=true;
  const wasLocked=document.body.classList.contains('lock');
  document.body.classList.remove('lock','popen'); document.body.style.top='';
  if(wasLocked) window.scrollTo(0,state.savedY);
  setTimeout(()=>{if(!state.cur){panel.hidden=true;pbody.innerHTML='';}},260);
  const card=cards.find(el=>el.dataset.id===id);
  if(card&&!card.hidden){
    const r=card.getBoundingClientRect();
    if(r.top<60||r.bottom>innerHeight) card.scrollIntoView({block:'center'});
  }
}
function requestClose(){
  if(history.state&&history.state.p) history.back();
  else{closePanel();history.replaceState(null,'',location.pathname+location.search);}
}
window.addEventListener('popstate',e=>{
  const p=(e.state&&e.state.p)||hashId();
  if(p&&DATA[p]) openPanel(p,{replace:true}); else closePanel();
});
$('#pclose').onclick=requestClose;
scrim.onclick=()=>{ if(!sortSheet.hidden) hideSort(); else if(state.cur) requestClose(); };

function updateNav(){
  const ids=visibleIds(), i=ids.indexOf(state.cur);
  $('#ppos').textContent=i>=0?`${i+1} / ${ids.length}`:'';
  $('#pprev').disabled=i<=0;
  $('#pnext').disabled=i<0||i>=ids.length-1;
}
function step(dir){const ids=visibleIds(), i=ids.indexOf(state.cur)+dir; if(ids[i]) openPanel(ids[i]);}
$('#pprev').onclick=()=>step(-1); $('#pnext').onclick=()=>step(1);
function preloadNext(id){
  const ids=visibleIds(), n=DATA[ids[ids.indexOf(id)+1]];
  const u=n&&n.photos&&n.photos[0]; if(u){const im=new Image();im.referrerPolicy='no-referrer';im.src=u;}
}

function render(d){
  const pics=[...(d.photos||[])];
  if(d.plan) pics.push(d.plan);
  const planIdx=d.plan?pics.length-1:-1;
  const gal=pics.length
    ?`<div class="gal" id="gal">${pics.map((u,i)=>`<a class="sl${i===planIdx?' plan':''}" href="${esc(u)}" target="_blank" rel="noopener"><img alt="" referrerpolicy="no-referrer" ${i===0?`src="${esc(u)}"`:`data-src="${esc(u)}"`}></a>`).join('')}</div>
     <div class="gbar"><span id="gcnt">1 / ${pics.length}</span>${planIdx>=0?`<button type="button" class="toplan" data-i="${planIdx}">📐 間取り図</button>`:''}</div>`
    :`<div class="gal none">写真なし</div>`;
  const kv=rows=>rows&&rows.length?`<dl class="kv">${rows.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`:'';
  const sec=(t,inner)=>inner?`<section class="sec"><h3>${esc(t)}</h3>${inner}</section>`:'';
  const walks=(d.walks||[]).map(([s,m,t])=>`<span class="wk${t?' tgt':''}">${esc(s)} 徒歩<b>${m}</b>分</span>`).join('');
  const sibs=(d.sibs||[]).map(x=>`<a class="sib" href="${esc(x.url)}" target="_blank" rel="noopener">${x.img?`<img loading="lazy" referrerpolicy="no-referrer" src="${esc(x.img)}" alt="">`:'<span class="sibno"></span>'}<span class="sibb"><b>${esc(x.price_s)}</b><span>${esc([x.floor,x.area?x.area+'㎡':'',x.shikirei,x.source].filter(Boolean).join('・'))}</span></span><span class="arw">↗</span></a>`).join('');
  const addr=d.addr?`<p>${esc(d.addr)}</p><a class="btn" href="https://maps.apple.com/?q=${encodeURIComponent(d.addr)}" target="_blank" rel="noopener">🗺 地図で開く</a>`:'<p>住所記載なし</p>';
  pbody.innerHTML=`${gal}
<div class="phd">
  <div class="ptag"><span class="tp ${esc(d.type)}">${esc(d.type_icon)} ${esc(d.type_label)}</span><span class="stn">${esc(d.station)}</span>${d.deal?'<span class="deal">⭐目玉</span>':''}${d.price_down?'<span class="dn">🔻値下げ</span>':''}${d.days&&d.days<=3?'<span class="new">NEW</span>':''}<span>${esc(d.source)}</span></div>
  <h2 class="pname">${esc(d.name||'建物名記載なし')}</h2>
  <div class="pprice">${esc(d.price_s)}${d.type==='rent'?'<small>管理費込</small>':''}</div>
  ${d.price_note?`<div class="pnote">${esc(d.price_note)}</div>`:''}
</div>
<div class="tiles">
  <div><b>${esc(d.area_s)}</b><small>面積</small></div>
  <div><b>${esc(d.layout||'—')}</b><small>間取り</small></div>
  <div><b>${d.walk!=null?`徒歩${d.walk}分`:'—'}</b><small>${esc(d.station)}</small></div>
  <div><b>${esc(d.age)}</b><small>${d.built?d.built+'年':'築年'}</small></div>
</div>
<a class="cta" href="${esc(d.url)}" target="_blank" rel="noopener">元ページを開く（${esc(d.source)}）↗</a>
${sec('費用',kv(d.cost))}
${sec('駐車場',`<p class="pk${d.parking?'':' unk'}">${esc(d.pk_label)}</p>`)}
${sec('駅からの徒歩',walks?`<div class="walks">${walks}</div>`:'')}
${sec('所在階',`<p>${esc(d.floor)}</p>`)}
${sec('住所',addr)}
${sec('諸元',kv(d.specs))}
${sec('掲載',`<p>${esc(d.listing_s)}</p>${d.hist_note?`<p>${esc(d.hist_note)}</p>`:''}${d.dup_note?`<p>${esc(d.dup_note)}</p>`:''}`)}
${(d.sibs||[]).length>1?sec(`同じ建物の掲載 ${d.sibs.length}件`,`<div class="sibs">${sibs}</div>`):''}
<a class="cta end" href="${esc(d.url)}" target="_blank" rel="noopener">元ページを開く ↗</a>`;
  $('#popen').href=d.url;
  setupGallery();
}

function setupGallery(){
  const gal=$('#gal'); if(!gal) return;
  const cnt=$('#gcnt');
  const load=sl=>{const im=sl&&sl.querySelector('img');if(im&&im.dataset.src){im.src=im.dataset.src;delete im.dataset.src;}};
  const cur=()=>Math.round(gal.scrollLeft/Math.max(1,gal.clientWidth));
  const slides=()=>$$('.sl',gal);
  load(slides()[1]);
  let raf=0;
  gal.addEventListener('scroll',()=>{
    if(raf) return;
    raf=requestAnimationFrame(()=>{raf=0;const s=slides(), i=cur();
      load(s[i]);load(s[i+1]);
      if(cnt) cnt.textContent=`${Math.min(i+1,s.length)} / ${s.length}`;});
  },{passive:true});
  const tp=$('.toplan',pbody);
  if(tp) tp.onclick=()=>{const s=slides(), i=s.findIndex(x=>x.classList.contains('plan'));
    if(i>=0){load(s[i]);gal.scrollTo({left:i*gal.clientWidth,behavior:'smooth'});}};
}

document.addEventListener('error',e=>{
  const im=e.target; if(!(im instanceof HTMLImageElement)) return;
  const sl=im.closest('.sl');
  if(sl){
    const gal=sl.parentElement; const wasPlan=sl.classList.contains('plan'); sl.remove();
    const n=$$('.sl',gal).length, c=$('#gcnt');
    if(wasPlan){const tp=$('.toplan',pbody); if(tp) tp.remove();}
    if(!n){gal.className='gal none';gal.textContent='写真なし';if(c)c.closest('.gbar').remove();}
    else if(c) c.textContent=`${Math.min(Math.round(gal.scrollLeft/Math.max(1,gal.clientWidth))+1,n)} / ${n}`;
    return;
  }
  if(im.closest('.thumb')){const ph=document.createElement('div');ph.className='noimg';ph.textContent='画像なし';im.replaceWith(ph);return;}
  if(im.closest('.sib')){const s=document.createElement('span');s.className='sibno';im.replaceWith(s);}
},true);

(()=>{
  const ph=$('.ph',panel); let y0=null, dy=0;
  ph.addEventListener('touchstart',e=>{if(isDesk())return;y0=e.touches[0].clientY;dy=0;panel.style.transition='none';},{passive:true});
  ph.addEventListener('touchmove',e=>{if(y0===null)return;dy=Math.max(0,e.touches[0].clientY-y0);panel.style.transform=`translateY(${dy}px)`;},{passive:true});
  ph.addEventListener('touchend',()=>{if(y0===null)return;panel.style.transition='';panel.style.transform='';if(dy>80)requestClose();y0=null;});
})();

window.addEventListener('keydown',e=>{
  if(e.key==='Escape'){if(!sortSheet.hidden)hideSort();else if(state.cur)requestClose();return;}
  if(!state.cur) return;
  if(e.key==='ArrowLeft') step(-1); else if(e.key==='ArrowRight') step(1);
});

apply();
const h0=hashId();
if(h0&&DATA[h0]){
  history.replaceState(null,'',location.pathname+location.search);
  openPanel(h0);
}else if(location.hash){history.replaceState(null,'',location.pathname+location.search);}
})();
"""


def build(items, out_path, station_order=None, reject_tally=None):
    """station_order: 駅タブの並び順。左から優先度の高い順に渡す。
    reject_tally: 条件で落とした理由の集計 (Counter)。ページ末尾に出す。"""
    jst = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    reject_html = ""
    if reject_tally:
        rows = "".join(
            f"<li>{html.escape(str(k))} … {v:,}件</li>"
            for k, v in reject_tally.most_common(12))
        total = sum(reject_tally.values())
        reject_html = (
            '<details class="rej"><summary>条件で落とした物件 '
            f'{total:,}件の内訳</summary><ul>{rows}</ul>'
            '<p>条件: 徒歩7分以内 / 3,000万〜1.2億 / 47㎡超 / 築20年未満 / '
            '賃料28万円以下(管理費込)</p></details>')
    present = {it["station"] for it in items}
    if station_order:
        # 指定された駅は物件が0件でもタブを出す（sho指示: 蛍池のタブは作っておく）
        stations = list(station_order)
        stations += sorted(present - set(stations))
    else:
        stations = sorted(present)
    types = ["mansion", "house", "land", "rent"]
    rank = {s: i for i, s in enumerate(stations)}

    def _rooms(it):
        m = _re.match(r"^(\d+)", it.get("layout") or "")
        if not m:
            return 0
        n = float(m.group(1))
        return n + 0.5 if "S" in (it.get("layout") or "").upper() else n

    def sort_key(it):
        # 目玉→値下げ→駅（タブ順）→売買(マンション→戸建→土地)→賃貸
        type_rank = {"mansion": 0, "house": 1, "land": 2, "rent": 3}
        return (0 if it.get("_deal") else 1,
                0 if it.get("_price_down") else 1,
                rank.get(it.get("station"), 999),
                type_rank.get(it.get("type"), 9),
                0 if _rooms(it) >= 2 else 1,
                0 if it.get("parking") in ("有", "近隣") else 1,
                it.get("walk") or 99)

    _before = len(items)
    items = [it for it in items if is_openable(it)]
    if len(items) < _before:
        print(f"開けないリンクの掲載を除外: {_before} → {len(items)}件")
    reps = []
    for g in group_items(items):
        rep = pick_rep(g)
        rep["_siblings"] = [x for x in g if x is not rep]
        reps.append(rep)
    reps = sorted(reps, key=sort_key)

    # 物件ID（URLハッシュ #p=ID に使う）。衝突したら長くする
    n = 8
    while len({pid_of(it, n) for it in reps}) != len(reps) and n < 40:
        n += 2
    pids = [pid_of(it, n) for it in reps]

    data, cards = {}, []
    host_cnt = {}
    for order, (it, pid) in enumerate(zip(reps, pids)):
        sibs = it.get("_siblings") or []
        allx = [it] + sibs
        data[pid] = item_json(it, pid, allx)
        photos = data[pid]["photos"]
        img = it.get("img") or ""
        if not img.startswith("http") and photos:
            img = photos[0]
        if img.startswith("http"):
            h = _re.match(r"https?://([^/]+)", img)
            if h:
                host_cnt[h.group(1)] = host_cnt.get(h.group(1), 0) + 1
        lazy = "" if order < 6 else "loading='lazy' "
        npics = len(photos) + (1 if data[pid]["plan"] else 0)
        badge = f"<span class=\"n\">📷{npics}</span>" if npics >= 2 else ""
        thumb = (f"<img {lazy}decoding='async' referrerpolicy='no-referrer' "
                 f"src='{html.escape(img)}' alt=''>{badge}"
                 if img.startswith("http") else "<div class='noimg'>画像なし</div>")
        t = it.get("type")
        if t == "land":
            cmp_parts = [data[pid]["area_s"], "更地/土地"]
        else:
            cmp_parts = [x for x in (data[pid]["area_s"], it.get("layout") or "間取り記載なし",
                                     age_text(it), it.get("floor") or "") if x]
        cmp = "".join(f"<span>{html.escape(x)}</span>" for x in cmp_parts)
        others = max(0, len(data[pid]["walks"]) - 1)
        walk = (f"{html.escape(it['station'])} 徒歩{it['walk']}分"
                if it.get("walk") is not None else "徒歩記載なし")
        more_st = f"<span class=\"more-st\">+{others}駅</span>" if others else ""
        rooms = {(round(x["area"], 1) if x.get("area") else None,
                  x.get("floor") or "", x.get("price")) for x in allx}
        rooms_note = (f'<span class="rooms">＋他{len(rooms) - 1}部屋</span>'
                      if len(rooms) > 1 else
                      (f'<span class="rooms">掲載{len(allx)}件</span>' if len(allx) > 1 else ""))
        tags = [f'<span class="tp">{TYPE_ICON.get(t, "")} {TYPE_LABEL.get(t, "")}</span>',
                f'<span class="stn">{html.escape(it["station"])}</span>']
        if it.get("_deal"):
            tags.append('<span class="deal">⭐目玉</span>')
        if it.get("_price_down"):
            tags.append('<span class="dn">🔻値下げ</span>')
        if it.get("_days") and it.get("_days") <= 3:
            tags.append('<span class="new">NEW</span>')
        price = it.get("price")
        cards.append(f"""<div class="card"
   data-id="{pid}" data-station="{html.escape(it['station'])}" data-type="{t or ''}"
   data-parking="{'1' if it.get('parking') in ('有','近隣') else '0'}"
   data-down="{'1' if it.get('_price_down') else '0'}"
   data-cut="{'1' if (it.get('_cuts') or 0) >= 1 else '0'}"
   data-rooms="{'1' if _rooms(it) >= 2 else '0'}"
   data-stale="{'1' if (it.get('_days') or 0) >= 90 else '0'}"
   data-deal="{'1' if it.get('_deal') else '0'}"
   data-price="{price if price is not None else ''}" data-area="{it.get('area') or ''}"
   data-walk="{it.get('walk') if it.get('walk') is not None else ''}"
   data-days="{it.get('_days') or 0}" data-range="{range_tag(it)}" data-ord="{order}">
 <a class="lnk" href="{html.escape(it['url'])}" target="_blank" rel="noopener">
  <div class="thumb">{thumb}</div>
  <div class="body">
    <div class="tag">{''.join(tags)}</div>
    <div class="price">{fmt_price(it)}</div>
    <div class="cmp">{cmp}</div>
    <div class="meta row"><span class="walk">{walk}{more_st}</span><span class="meta pk">{fmt_parking(it)}</span></div>
    <div class="name">{html.escape(tidy_name(it.get('name') or '')[:40])}<span class="src">{html.escape(it.get('source', ''))}</span>{rooms_note}</div>
  </div>
 </a>
</div>""")

    chips = "".join(
        f"<button class='chip' data-f='station' data-v='{html.escape(s)}'>{html.escape(s)}</button>"
        for s in stations)
    tchips = "".join(
        f"<button class='chip' data-f='type' data-v='{t}'>{TYPE_ICON[t]}{TYPE_LABEL[t]}</button>"
        for t in types)
    preconnect = "".join(
        f'<link rel="preconnect" href="https://{h}">'
        for h, _ in sorted(host_cnt.items(), key=lambda kv: -kv[1])[:3])
    nrent = sum(1 for it in reps if it.get("type") == "rent")
    N = len(cards)

    doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="物件在庫">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#faf9f7" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#141413" media="(prefers-color-scheme:dark)">
{preconnect}
<title>物件在庫 {N}件</title>
<style>{CSS}</style></head><body>
<header id="hd">
  <div class="row1">
    <h1>物件在庫</h1>
    <div class="count"><b><span id="shown">{N}</span></b> / {N}物件 <span id="bd">売買{N - nrent} / 賃貸{nrent}</span>
      <span class="ts">掲載{len(items)}件・{jst:%m/%d %H:%M}更新</span></div>
    <button type="button" id="sortbtn" aria-haspopup="true">⇅ おすすめ</button>
  </div>
  <div class="filters stn">{chips}</div>
  <div class="filters cond">
    <button class="chip" data-f="deal" data-v="1">⭐目玉</button><button class="chip" data-f="down" data-v="1">🔻値下げ</button><button class="chip" data-f="rooms" data-v="1">2LDK以上</button><button class="chip" data-f="parking" data-v="1">🚗駐車場あり</button><button class="chip" data-f="range" data-v="r20">〜20万/月</button><button class="chip" data-f="range" data-v="b6">〜6,000万</button><button class="chip" data-f="cut" data-v="1">値下げ実績</button><button class="chip" data-f="stale" data-v="1">90日以上</button><span class="vr"></span>{tchips}
  </div>
  <div id="active-bar" hidden><span id="active-txt"></span>
    <button type="button" id="clear">✕ クリア</button></div>
</header>
<main id="grid">
{''.join(cards)}
<div class="empty" id="empty" hidden>条件に合う物件がありません
  <button type="button" id="clear2">条件を全部外す</button></div>
{reject_html}
</main>
<div id="scrim" hidden></div>
<section id="panel" class="panel" role="dialog" aria-modal="true" aria-label="物件の詳細" hidden>
  <div class="ph"><span id="ppos"></span><span class="grab"></span>
    <button type="button" class="x" id="pclose" aria-label="閉じる">×</button></div>
  <div class="pb" id="pbody"></div>
  <div class="pf">
    <button type="button" id="pprev">‹ 前</button>
    <a id="popen" class="pri" href="#" target="_blank" rel="noopener">元ページを開く ↗</a>
    <button type="button" id="pnext">次 ›</button>
  </div>
</section>
<div id="sortsheet" class="sheet" role="menu" hidden>
  <button type="button" data-sort="rec" class="on">おすすめ</button>
  <button type="button" data-sort="price_asc">価格が安い順</button>
  <button type="button" data-sort="price_desc">価格が高い順</button>
  <button type="button" data-sort="area">広い順</button>
  <button type="button" data-sort="walk">駅が近い順</button>
  <button type="button" data-sort="new">新着順</button>
</div>
<script type="application/json" id="data">{dump_json(data)}</script>
<script>{JS}</script>
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return N
