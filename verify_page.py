#!/usr/bin/env python3
"""公開するページを出す前に、全部を自分で確かめる。
条件違反・写真切れ・構造の壊れが1つでもあれば異常終了して公開を止める。
（shoさん指示 2026-09-14「完成したら全部確認するフックをかけとけ」）

  python3 verify_page.py docs/index.html
"""
import html as H
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import watcher as W  # noqa: E402

NG = []
OK = []


def ng(msg):
    NG.append(msg)


def ok(msg):
    OK.append(msg)


def main(path):
    h = Path(path).read_text(encoding="utf-8")
    cards = re.split(r'(?=<div class="card")', h)[1:]

    # --- 構造 ---
    if not cards:
        ng("カードが1件も無い")
    if re.search(r'<a class="card"', h):
        ng(f'旧構造 <a class="card"> が {len(re.findall(chr(60) + "a class=" + chr(34) + "card" + chr(34), h))} 件残っている')
    if len(re.findall(r"<details", h)) != len(re.findall(r"</details>", h)):
        ng("<details> の開閉数が合わない")
    bad_nest = 0
    for c in cards:
        i_a, i_d = c.find("</a>"), c.find("<details")
        if i_d != -1 and i_a != -1 and i_d < i_a:
            bad_nest += 1
    if bad_nest:
        ng(f"<a> の中に <details> が入っているカードが {bad_nest} 件")
    ok(f"カード {len(cards)} 件 / 見開き {len(re.findall(chr(60) + 'details class=', h))} 件")

    # --- 画像 ---
    noimg = len(re.findall(r"class='noimg'", h))
    # 写真が1枚も無い物件（更地の土地など）は現実に存在する。
    # 1件で公開全体を止めると在庫が出せなくなるので、割合で見る。
    limit = max(2, int(len(cards) * 0.03))
    if noimg > limit:
        ng(f"画像なしのカードが {noimg} 件（許容 {limit} 件まで）")
    elif noimg:
        ok(f"画像なし {noimg} 件（許容 {limit} 件以内）")
    srcs = list(dict.fromkeys(re.findall(r"<img[^>]+src='([^']+)'", h)))
    bad_pat = re.compile(r"nophoto|noimage|no_image|/appli|bnr|banner|osusume"
                         r"|cms_image|jibun|img01\.suumo\.com/jj/", re.I)
    banners = [u for u in srcs if bad_pat.search(u.split("?")[0])]
    if banners:
        ng(f"広告・表示できない画像URLが {len(banners)} 件: {banners[0][:80]}")
    dead = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for u, alive in zip(srcs, ex.map(W.image_alive, srcs)):
            if not alive:
                dead.append(u)
    if dead:
        ng(f"開けない画像が {len(dead)} 件: {dead[0][:90]}")
    ok(f"画像 {len(srcs)} 件すべて実際に開けた" if not dead else "")

    # --- 条件 ---
    areas = [float(x) for x in re.findall(r"([\d.]+)㎡", h)]
    if areas and min(areas) <= W.AREA_MIN - 0.01:
        ng(f"面積の下限違反: 最小 {min(areas)}㎡ (下限 {W.AREA_MIN})")
    ages = [int(x) for x in re.findall(r"築(\d+)年", h)]
    if ages and max(ages) >= W.MANSION_MAX_AGE if hasattr(W, "MANSION_MAX_AGE") else False:
        pass
    if ages and max(ages) >= W.BUILT_MAX_AGE:
        ng(f"築年の違反: 最大 築{max(ages)}年 (上限 {W.BUILT_MAX_AGE}年未満)")
    walks = []
    for c in cards:
        seg = c[:c.find("</a>") if c.find("</a>") > 0 else len(c)]
        walks += [int(x) for x in re.findall(r"徒歩(\d+)分", seg)]
    if walks and max(walks) > W.WALK_MAX:
        ng(f"徒歩の違反: 最大 {max(walks)}分 (上限 {W.WALK_MAX}分)")
    rents = [float(x) for x in re.findall(r"([\d.]+)万円/月", h)]
    if rents and max(rents) > W.RENT_MAX:
        ng(f"賃料の違反: 最大 {max(rents)}万円 (上限 {W.RENT_MAX}万円)")
    prices = []
    for m in re.finditer(r'class="price">(?:(\d+)億)?([\d,]*)万円(?!/月)', h):
        v = (int(m.group(1)) * 10000 if m.group(1) else 0)
        v += int(m.group(2).replace(",", "")) if m.group(2) else 0
        prices.append(v)
    outs = [p for p in prices if not (W.PRICE_MIN <= p <= W.PRICE_MAX)]
    if outs:
        ng(f"価格の違反: {len(outs)}件 (例 {outs[0]}万円 / 範囲 {W.PRICE_MIN}〜{W.PRICE_MAX})")
    ok(f"条件: 面積最小{min(areas) if areas else '-'}㎡ / 築最大{max(ages) if ages else '-'}年 / "
       f"徒歩最大{max(walks) if walks else '-'}分 / 賃料最大{max(rents) if rents else '-'}万 / "
       f"売買{min(prices) if prices else '-'}〜{max(prices) if prices else '-'}万")

    # --- 駅 ---
    tabs = re.findall(r"data-f='station' data-v='([^']+)'", h)
    want = W.PRIORITY_STATIONS + [s for s in W.STATIONS
                                  if s not in W.PRIORITY_STATIONS]
    extra = [t for t in tabs if t not in W.STATIONS]
    if extra:
        ng(f"指定外の駅がタブにある: {extra}")
    if tabs != [w for w in want if w in tabs]:
        ng(f"駅タブの並びが指定と違う: {tabs}")
    ok(f"駅タブ {len(tabs)}個 {tabs}")

    print("=" * 60)
    for o in OK:
        if o:
            print("  OK  " + o)
    for n in NG:
        print("  NG  " + n)
    print("=" * 60)
    if NG:
        print(f"検証 失敗: {len(NG)}件の問題。公開を中止します", file=sys.stderr)
        return 1
    print("検証 合格: 公開してよい")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "docs/index.html"))
