"""プール全件を1枚のHTMLにする（毎朝の通知とは別。今ある在庫を全部見るため）"""
import html
import json
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
        # 指定された駅は物件が0件でもタブを出す（sho指示: 蛍池のタブは
        # 作っておく）。その日たまたま0件でも、タブが消えると
        # 「対象から外れた」ように見えてしまう
        stations = list(station_order)
        stations += sorted(present - set(stations))   # 順序指定に無い駅は末尾
    else:
        stations = sorted(present)
    types = ["mansion", "house", "land", "rent"]

    # カードもタブと同じ優先順（左＝上）に並べる。駅内は駐車場あり→駅近。
    rank = {s: i for i, s in enumerate(stations)}

    def _rooms(it):
        import re as _re
        m = _re.match(r"^(\d+)", it.get("layout") or "")
        if not m:
            return 0
        n = float(m.group(1))
        return n + 0.5 if "S" in (it.get("layout") or "").upper() else n

    def sort_key(it):
        # 駅で絞ったときに賃貸と売買が混ざらないよう、駅の中では
        # 売買（マンション→戸建→土地）を先に、賃貸を後ろにまとめる
        type_rank = {"mansion": 0, "house": 1, "land": 2, "rent": 3}
        return (0 if it.get("_price_down") else 1,
                rank.get(it.get("station"), 999),
                type_rank.get(it.get("type"), 9),
                0 if _rooms(it) >= 2 else 1,   # 2LDK以上を上に
                0 if it.get("parking") in ("有", "近隣") else 1,
                it.get("walk") or 99)

    groups = group_items(items)
    reps = []
    for g in groups:
        rep = pick_rep(g)
        rep["_siblings"] = [x for x in g if x is not rep]
        reps.append(rep)

    def _sub_row(x):
        """見開きに出す1掲載ぶんの行。値段・階・敷礼が掲載ごとに違う。"""
        if x.get("type") == "land":
            fl = "階なし(土地)"
        else:
            fl = html.escape(x.get("floor") or "") or "階記載なし"
        sr = html.escape(x.get("shikirei") or "") if x.get("type") == "rent" else ""
        im = x.get("img") or ""
        th = (f"<img loading='lazy' src='{html.escape(im)}' alt=''>"
              if im.startswith("http") else "<span class='sub-noimg'>画像なし</span>")
        return (f"<a class='sub-row' href=\"{html.escape(x['url'])}\" target=\"_blank\" "
                f"rel=\"noopener\"><span class='sub-th'>{th}</span>"
                f"<span class='sub-b'><b>{fmt_price(x)}</b>"
                f"<span class='sub-m'>{fl}"
                + (f"・{sr}" if sr else "")
                + f"・{html.escape(x.get('source',''))}</span>"
                f"<span class='sub-n'>{html.escape((x.get('name') or '')[:38])}</span>"
                "</span></a>")

    cards = []
    for it in sorted(reps, key=sort_key):
        sibs = it.get("_siblings") or []
        img = it.get("img") or ""
        thumb = (f"<img loading='lazy' src='{html.escape(img)}' alt=''>"
                 if img else "<div class='noimg'>画像なし</div>")
        built = it.get("built")
        if it.get("type") == "land":
            age = "更地/土地"
        elif built:
            yrs = CURRENT_YEAR - built
            age = "新築" if yrs <= 0 else f"築{yrs}年"
        else:
            age = "築年記載なし"
        area = f"{it['area']}㎡" if it.get("area") else "面積記載なし"
        # 所在階は全物件に出す。取れなければ「階記載なし」と明記して伏せない
        if it.get("type") == "land":
            floor = "階なし(土地)"
        else:
            floor = html.escape(it.get("floor") or "") or "階記載なし"
        layout = html.escape(it.get("layout") or "")
        if not layout:
            layout = "" if it.get("type") == "land" else "間取り記載なし"
        rooms_note = ""
        note = html.escape(it.get("_dup_note") or "")
        pnote = html.escape(it.get("_price_note") or "")
        sr = html.escape(it.get("shikirei") or "") if it.get("type") == "rent" else ""
        hnote = html.escape(it.get("_hist_note") or "")
        # 同じ物件の他の掲載。値段・階が違うので畳んで全部出す
        allx = [it] + sibs
        prices = [x.get("price") for x in allx if x.get("price") is not None]
        unit = "万円/月" if it.get("type") == "rent" else "万円"
        # 同じ建物でも階や面積が違えば別の部屋。何部屋ぶんあるか出さないと
        # 代表カードの面積・階が全体を代表しているように見えてしまう
        rooms = {(round(x["area"], 1) if x.get("area") else None,
                  x.get("floor") or "", x.get("price")) for x in allx}
        bits = []
        if len(rooms) > 1:
            bits.append(f"{len(rooms)}部屋")
        if prices and min(prices) != max(prices):
            bits.append(f"{min(prices)}〜{max(prices)}{unit}")
        rng = f"（{'・'.join(bits)}）" if bits else ""
        more = ""
        if sibs:
            label = "同じ建物の掲載" if len(rooms) > 1 else "同じ物件の掲載"
            # 階→価格の順に並べると部屋ごとに読める
            def _ord(x):
                import re as _r
                m = _r.search(r"(\d+)", x.get("floor") or "")
                return (int(m.group(1)) if m else 999,
                        x.get("price") if x.get("price") is not None else 9e9)
            more = (f'<details class="more"><summary>{label} '
                    f'{len(allx)}件{rng}</summary><div class="subs">'
                    + "".join(_sub_row(x) for x in sorted(allx, key=_ord))
                    + "</div></details>")
        if len(rooms) > 1:
            rooms_note = f'<span class="rooms">＋他{len(rooms) - 1}部屋</span>'
        cards.append(f"""<div class="card"
   data-station="{html.escape(it['station'])}" data-type="{it.get('type','')}"
   data-parking="{'1' if it.get('parking') in ('有','近隣') else '0'}"
   data-down="{'1' if it.get('_price_down') else '0'}"
   data-cut="{'1' if (it.get('_cuts') or 0) >= 1 else '0'}"
   data-rooms="{'1' if _rooms(it) >= 2 else '0'}"
   data-stale="{'1' if (it.get('_days') or 0) >= 90 else '0'}">
 <a class="lnk" href="{html.escape(it['url'])}" target="_blank" rel="noopener">
  <div class="thumb">{thumb}</div>
  <div class="body">
    <div class="tag">{TYPE_ICON.get(it.get('type'),'')} {TYPE_LABEL.get(it.get('type'),'')}
      <span class="stn">{html.escape(it['station'])}</span>
      <span class="src">{html.escape(it.get('source',''))}</span></div>
    <div class="name">{html.escape((it.get('name') or '')[:44])}</div>
    <div class="price">{fmt_price(it)}</div>
    <div class="meta">{"・".join(x for x in (layout, area, floor, age) if x)}{rooms_note}</div>
    <div class="meta">{fmt_walk(it)}</div>
    {f'<div class="meta">{sr}</div>' if sr else ''}
    <div class="meta pk">{fmt_parking(it)}</div>
    <div class="meta addr">{html.escape(it.get('addr') or '住所記載なし')}</div>
    {f'<div class="meta down">{pnote}</div>' if pnote else ''}
    {f'<div class="meta hist">{hnote}</div>' if hnote else ''}
    {f'<div class="meta dup">{note}</div>' if note else ''}
  </div>
 </a>
 {more}
</div>""")

    chips = "".join(
        f"<button class='chip' data-f='station' data-v='{html.escape(s)}'>{html.escape(s)}</button>"
        for s in stations)
    tchips = "".join(
        f"<button class='chip' data-f='type' data-v='{t}'>{TYPE_ICON[t]}{TYPE_LABEL[t]}</button>"
        for t in types)

    doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="物件在庫">
<meta name="theme-color" content="#faf9f7" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#141413" media="(prefers-color-scheme:dark)">
<title>物件在庫 {len(cards)}件</title>
<style>
:root{{--bg:#faf9f7;--fg:#1c1b19;--sub:#6b6862;--line:#e6e3dd;--card:#fff;--accent:#1a5d3a;--buy:#1a5d3a;--rent:#b4560a}}
@media(prefers-color-scheme:dark){{:root{{--bg:#141413;--fg:#f0eee9;--sub:#a3a099;--line:#2c2b28;--card:#1c1b19;--buy:#4ea87a;--rent:#e59a4d}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;line-height:1.6}}
header{{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
  padding:14px 16px 10px;z-index:10}}
h1{{margin:0 0 2px;font-size:17px;letter-spacing:.02em}}
.count{{color:var(--sub);font-size:13px}}
.filters{{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}}
.chip{{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;
  padding:5px 11px;font-size:12.5px;cursor:pointer;font-family:inherit}}
.chip.on{{background:var(--accent);color:#fff;border-color:var(--accent)}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:12px;padding:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
  overflow:hidden;color:inherit}}
.card:hover{{border-color:var(--accent)}}
/* 駅で絞ったとき、賃貸と売買を一目で見分けられるように左端で色分けする */
.card{{border-left:4px solid var(--buy)}}
.card[data-type="rent"]{{border-left-color:var(--rent)}}
.tag{{font-weight:600}}
.card[data-type="rent"] .tag{{color:var(--rent)}}
.card:not([data-type="rent"]) .tag{{color:var(--buy)}}
.lnk{{display:flex;gap:11px;text-decoration:none;color:inherit}}
/* 同じ物件の別掲載。値段・階が違うので畳んで全部出す */
.more{{border-top:1px solid var(--line)}}
.more summary{{cursor:pointer;font-size:11.5px;color:var(--accent);
  padding:7px 11px;font-weight:600;list-style:none}}
.more summary::-webkit-details-marker{{display:none}}
.more summary::before{{content:"▸ ";font-weight:400}}
.more[open] summary::before{{content:"▾ "}}
.subs{{padding:0 8px 8px}}
.sub-row{{display:flex;gap:8px;align-items:center;padding:6px;border-radius:8px;
  text-decoration:none;color:inherit}}
.sub-row:hover{{background:var(--bg)}}
.sub-th{{width:52px;height:40px;flex:none;background:var(--line);border-radius:5px;
  overflow:hidden;display:flex;align-items:center;justify-content:center}}
.sub-th img{{width:52px;height:40px;object-fit:cover;display:block}}
.sub-noimg{{font-size:9px;color:var(--sub)}}
.sub-b{{min-width:0;flex:1;display:flex;flex-direction:column;line-height:1.35}}
.sub-b b{{font-size:13px}}
.sub-b .sub{{font-size:10px}}
.sub-m{{font-size:11px;color:var(--sub)}}
.sub-n{{font-size:10.5px;color:var(--sub);opacity:.75;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
.thumb{{width:104px;flex:none;background:var(--line)}}
.thumb img{{width:104px;height:100%;object-fit:cover;display:block}}
.noimg{{width:104px;height:100%;min-height:120px;display:flex;align-items:center;justify-content:center;
  color:var(--sub);font-size:11px}}
.body{{padding:10px 11px 11px 0;min-width:0;flex:1}}
.tag{{font-size:11px;color:var(--sub);margin-bottom:3px}}
.stn{{color:var(--accent);font-weight:600;margin-left:4px}}
.src{{margin-left:4px;opacity:.7}}
.name{{font-size:13px;font-weight:600;margin-bottom:3px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.price{{font-size:16px;font-weight:700;margin-bottom:4px}}
.price .sub{{font-size:11px;font-weight:400;color:var(--sub)}}
.meta{{font-size:11.5px;color:var(--sub)}}
.pk{{color:var(--fg)}}
.dup{{color:var(--accent)}}
.rooms{{color:var(--accent);font-weight:600;margin-left:5px}}
.down{{color:#c0392b;font-weight:600}}
.hist{{color:#8a6d1f}}
@media(prefers-color-scheme:dark){{.hist{{color:#d8b74a}}}}
@media(prefers-color-scheme:dark){{.down{{color:#ff7a6b}}}}
.empty{{padding:40px 16px;color:var(--sub);text-align:center;grid-column:1/-1}}
.rej{{margin:18px 8px;padding:10px 14px;background:var(--card);border:1px solid var(--line);border-radius:10px;font-size:13px;color:var(--sub);grid-column:1/-1}}
.rej summary{{cursor:pointer;font-weight:600;color:var(--fg)}}
.rej ul{{margin:8px 0 4px;padding-left:20px}}
.rej li{{margin:2px 0}}
.rej p{{margin:6px 0 0;opacity:.8;font-size:12px}}
/* iPhone: 1カラム、フィルタは横スクロール、余白を詰める */
@media(max-width:640px){{
  header{{padding:11px 12px 8px;padding-left:max(12px,env(safe-area-inset-left));
    padding-right:max(12px,env(safe-area-inset-right))}}
  h1{{font-size:15px}}
  .filters{{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;
    scrollbar-width:none;margin-top:8px;padding-bottom:2px}}
  .filters::-webkit-scrollbar{{display:none}}
  .chip{{flex:none;padding:6px 12px;font-size:13px}}
  main{{grid-template-columns:1fr;gap:9px;padding:12px;
    padding-bottom:max(12px,env(safe-area-inset-bottom))}}
  .thumb,.thumb img,.noimg{{width:92px}}
  .price{{font-size:15px}}
  .name{{font-size:12.5px}}
}}
</style></head><body>
<header>
  <h1>物件在庫</h1>
  <div class="count"><span id="shown">{len(cards)}</span> / {len(cards)}物件
    <span id="bd"></span>（掲載{len(items)}件）　{jst:%Y-%m-%d %H:%M} JST時点</div>
  <div class="filters">
    <button class="chip" data-f="down" data-v="1">🔻値下げ</button><button class="chip" data-f="rooms" data-v="1">2LDK以上</button><button class="chip" data-f="cut" data-v="1">値下げ実績</button><button class="chip" data-f="stale" data-v="1">90日以上</button><button class="chip" data-f="parking" data-v="1">🚗駐車場あり</button>{tchips}
  </div>
  <div class="filters">{chips}</div>
</header>
<main id="grid">
{''.join(cards)}
<div class="empty" id="empty" style="display:none">条件に合う物件がありません</div>
{reject_html}
</main>
<script>
const active={{}};
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{{
  const f=c.dataset.f,v=c.dataset.v;
  if(active[f]===v){{delete active[f];c.classList.remove('on');}}
  else{{
    document.querySelectorAll(`.chip[data-f="${{f}}"]`).forEach(o=>o.classList.remove('on'));
    active[f]=v;c.classList.add('on');
  }}
  let n=0;
  document.querySelectorAll('.card').forEach(el=>{{
    const ok=Object.entries(active).every(([k,val])=>el.dataset[k]===val);
    el.style.display=ok?'':'none'; if(ok)n++;
  }});
  document.getElementById('shown').textContent=n;
  // 絞り込み中の内訳（売買/賃貸）を出す
  let buy=0,rent=0;
  document.querySelectorAll('.card').forEach(el=>{{
    if(el.style.display==='none')return;
    if(el.dataset.type==='rent')rent++;else buy++;
  }});
  document.getElementById('bd').textContent=
    (buy||rent)?` ｜ 売買${{buy}} / 賃貸${{rent}}`:'';
  document.getElementById('empty').style.display=n?'none':'';
}});
(function(){{
  let buy=0,rent=0;
  document.querySelectorAll('.card').forEach(el=>{{
    if(el.dataset.type==='rent')rent++;else buy++;
  }});
  document.getElementById('bd').textContent=` ｜ 売買${{buy}} / 賃貸${{rent}}`;
}})();
</script></body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return len(cards)
