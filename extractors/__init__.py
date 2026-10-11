"""詳細ページから写真(photos)・間取り図(plan)・諸元(specs)を取る。
サイトごとにページの作りが違うので、取得元(source)で振り分ける。
取れなくても例外は外に出さず空で返す（物件を落とさない）。"""
import copy
import importlib
import re

# サイトのロゴ・「画像なし」・バナー等。物件の写真ではない
# （実測: CHINTAIの一覧サムネが brand.png のロゴだった）
BAD_IMG = re.compile(r"(brand\.png|/logo|logo[_.-]|noimage|no_image|nophoto|no_photo|No_Photo"
                     r"|spacer|blank\.gif|clear\.gif|bnr|banner|/appli)", re.I)

_MAP = {
    "SUUMO": "suumo", "SUUMO賃貸": "suumo_rent", "goo住宅": "goo",
    "ハウスコム": "housecom", "CHINTAI": "chintai", "賃貸スモッカ": "smocca",
    "リバブル": "livable", "三井のリハウス": "rehouse",
    "スマイティ賃貸": "sumaity_rent", "スマイティ": "sumaity",
}


def extract_detail(source, soup, url):
    mod = _MAP.get(source)
    if not mod:
        return {"photos": [], "plan": None, "specs": {}}
    try:
        m = importlib.import_module(f"{__name__}.{mod}")
        # 抽出側が要素を消す(decompose)ので、呼び出し元の soup は触らせない。
        # 実測(2026-10-11): 触らせたせいで徒歩・駐車場・築年が読めなくなり、
        # 条件を満たす物件104件が「対象駅なし」で落ちた
        r = m.extract(copy.copy(soup), url) or {}
    except Exception as e:  # ページの作りが変わっても全体は止めない
        print(f"   詳細の抽出に失敗({source}): {e}")
        return {"photos": [], "plan": None, "specs": {}}
    photos, seen = [], set()
    for u in r.get("photos") or []:
        if (isinstance(u, str) and u.startswith("http") and u not in seen
                and not BAD_IMG.search(u.split("?")[0])):
            seen.add(u); photos.append(u)
    specs = {str(k): str(v) for k, v in (r.get("specs") or {}).items() if k and v}
    plan = r.get("plan") if isinstance(r.get("plan"), str) and r.get("plan", "").startswith("http") else None
    if plan and BAD_IMG.search(plan.split("?")[0]):
        plan = None
    return {"photos": photos[:20], "plan": plan, "specs": specs}
