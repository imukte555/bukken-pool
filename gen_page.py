"""プール全件を1枚のHTMLにする（毎朝の通知とは別。今ある在庫を全部見るため）"""
import html
import json
from datetime import datetime, timezone, timedelta

TYPE_LABEL = {"mansion": "マンション", "house": "戸建", "land": "土地", "rent": "賃貸"}
TYPE_ICON = {"mansion": "🏢", "house": "🏠", "land": "🏞", "rent": "🔑"}
CURRENT_YEAR = 2026


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
             "空無": "🚗駐車場あり(空きなし)", "無": "駐車場なし"}.get(pk, "駐車場記載なし")
    pp = it.get("parking_price")
    if pp and pk in ("有", "近隣", "空無"):
        label += f" {html.escape(pp)}"
    return label


def build(items, out_path, station_order=None):
    """station_order: 駅タブの並び順。左から優先度の高い順に渡す。"""
    jst = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    present = {it["station"] for it in items}
    if station_order:
        stations = [s for s in station_order if s in present]
        stations += sorted(present - set(stations))   # 順序指定に無い駅は末尾
    else:
        stations = sorted(present)
    types = ["mansion", "house", "land", "rent"]

    # カードもタブと同じ優先順（左＝上）に並べる。駅内は駐車場あり→駅近。
    rank = {s: i for i, s in enumerate(stations)}

    def sort_key(it):
        return (rank.get(it.get("station"), 999),
                0 if it.get("parking") in ("有", "近隣") else 1,
                it.get("walk") or 99)

    cards = []
    for it in sorted(items, key=sort_key):
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
        layout = html.escape(it.get("layout") or "")
        if not layout:
            layout = "" if it.get("type") == "land" else "間取り記載なし"
        note = html.escape(it.get("_dup_note") or "")
        cards.append(f"""<a class="card" href="{html.escape(it['url'])}" target="_blank" rel="noopener"
   data-station="{html.escape(it['station'])}" data-type="{it.get('type','')}"
   data-parking="{'1' if it.get('parking') in ('有','近隣') else '0'}">
  <div class="thumb">{thumb}</div>
  <div class="body">
    <div class="tag">{TYPE_ICON.get(it.get('type'),'')} {TYPE_LABEL.get(it.get('type'),'')}
      <span class="stn">{html.escape(it['station'])}</span>
      <span class="src">{html.escape(it.get('source',''))}</span></div>
    <div class="name">{html.escape((it.get('name') or '')[:44])}</div>
    <div class="price">{fmt_price(it)}</div>
    <div class="meta">{"・".join(x for x in (layout, area, age) if x)}</div>
    <div class="meta">{fmt_walk(it)}</div>
    <div class="meta pk">{fmt_parking(it)}</div>
    <div class="meta addr">{html.escape(it.get('addr') or '住所記載なし')}</div>
    {f'<div class="meta dup">{note}</div>' if note else ''}
  </div>
</a>""")

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
<title>物件在庫 {len(items)}件</title>
<style>
:root{{--bg:#faf9f7;--fg:#1c1b19;--sub:#6b6862;--line:#e6e3dd;--card:#fff;--accent:#1a5d3a}}
@media(prefers-color-scheme:dark){{:root{{--bg:#141413;--fg:#f0eee9;--sub:#a3a099;--line:#2c2b28;--card:#1c1b19}}}}
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
.card{{display:flex;gap:11px;background:var(--card);border:1px solid var(--line);border-radius:12px;
  overflow:hidden;text-decoration:none;color:inherit}}
.card:hover{{border-color:var(--accent)}}
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
.empty{{padding:40px 16px;color:var(--sub);text-align:center;grid-column:1/-1}}
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
  <div class="count"><span id="shown">{len(items)}</span> / {len(items)}件　{jst:%Y-%m-%d %H:%M} JST時点</div>
  <div class="filters">
    <button class="chip" data-f="parking" data-v="1">🚗駐車場あり</button>{tchips}
  </div>
  <div class="filters">{chips}</div>
</header>
<main id="grid">
{''.join(cards)}
<div class="empty" id="empty" style="display:none">条件に合う物件がありません</div>
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
  document.getElementById('empty').style.display=n?'none':'';
}});
</script></body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return len(items)
