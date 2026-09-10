#!/usr/bin/env python3
"""HOMES / アットホーム / ニフティ不動産 をローカル(Mac)から取って
local_feed.json に書き出し、リポジトリへpushする。

GitHub Actions のIPからはこの3サイトが拒否される（実測:
HOMES 通算6回で打ち切り / アットホーム 4回 / ニフティ 8回）。
ローカルからは普通に取れるので、ここで取ってリポジトリに置き、
Actions 側の watcher.py が collect_all() で取り込む。

使い方:
    python3 local_fetch.py            # 取得して local_feed.json を書く
    python3 local_fetch.py --push     # 書いたあと git push まで
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import watcher as W  # noqa: E402

BASE = Path(__file__).parent
OUT = BASE / "local_feed.json"


def collect():
    items = []
    for st, c in W.STATIONS.items():
        pref = c.get("pref", "tokyo")
        # --- HOMES 売買3種 + 賃貸 ---
        if c.get("homes"):
            for kind, path in [
                ("mansion", f"mansion/chuko/{pref}/{c['homes']}/list/"),
                ("house",   f"kodate/chuko/{pref}/{c['homes']}/list/"),
                ("land",    f"tochi/{pref}/{c['homes']}/list/"),
            ]:
                for pn in (1, 2, 3):
                    h = W.fetch_with_retry(
                        f"https://www.homes.co.jp/{path}?page={pn}",
                        impersonate=True)
                    got = W.parse_homes(h, st, kind)
                    if not got:
                        break
                    items.extend(W.filter_with_walk_rescue(got))
                    time.sleep(W.SLEEP_BETWEEN)
            for pn in (1, 2, 3):
                h = W.fetch_with_retry(
                    f"https://www.homes.co.jp/chintai/{pref}/{c['homes']}/list/?page={pn}",
                    impersonate=True)
                got = W.parse_homes_rent(h, st)
                if not got:
                    break
                items.extend([i for i in got if W.apply_rent_filters(i)])
                time.sleep(W.SLEEP_BETWEEN)
        # --- アットホーム 売買3種 + 賃貸 ---
        if c.get("athome"):
            for kind, path in [
                ("mansion", f"mansion/chuko/{pref}/{c['athome']}/list/"),
                ("house",   f"kodate/{pref}/{c['athome']}/list/"),
                ("land",    f"tochi/{pref}/{c['athome']}/list/"),
            ]:
                for pn in (1, 2, 3):
                    h = W.fetch_with_retry(
                        f"https://www.athome.co.jp/{path}?page={pn}",
                        impersonate=True)
                    got = W.parse_athome(h, st, kind)
                    if not got:
                        break
                    items.extend(W.filter_with_walk_rescue(got))
                    time.sleep(W.SLEEP_BETWEEN)
            for pn in (1, 2, 3):
                h = W.fetch_with_retry(
                    f"https://www.athome.co.jp/chintai/{pref}/{c['athome']}/list/?page={pn}",
                    impersonate=True)
                got = W.parse_athome_rent(h, st)
                if not got:
                    break
                items.extend([i for i in got if W.apply_rent_filters(i)])
                time.sleep(W.SLEEP_BETWEEN)
        # --- ニフティ 6種別 ---
        if c.get("nifty"):
            for kind, path in [
                ("mansion", f"chuko-mansion/{pref}/{c['nifty']}_st/"),
                ("house",   f"chuko-ikkodate/{pref}/{c['nifty']}_st/"),
                ("land",    f"tochi/{pref}/{c['nifty']}_st/"),
                ("rent",    f"rent/{pref}/{c['nifty']}_st/"),
                ("mansion", f"shinchiku-mansion/{pref}/{c['nifty']}_st/"),
                ("house",   f"shinchiku-ikkodate/{pref}/{c['nifty']}_st/"),
            ]:
                for pn in (1, 2, 3):
                    url = (f"https://myhome.nifty.com/{path}"
                           if pn == 1 else
                           f"https://myhome.nifty.com/{path}{pn}/")
                    h = W.fetch_with_retry(url, impersonate=True)
                    got = W.parse_nifty(h, st, kind)
                    if not got:
                        break
                    items.extend([i for i in got if W.apply_rent_filters(i)]
                                 if kind == "rent"
                                 else W.filter_with_walk_rescue(got))
                    time.sleep(W.SLEEP_BETWEEN)
        print(f"  {st}: 累計 {len(items)}件", flush=True)
    return items


def main():
    items = collect()
    # 同一idの重複を落とす
    seen, uniq = set(), []
    for it in items:
        if it.get("id") in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
    jst = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    OUT.write_text(json.dumps(
        {"generated_at": jst.strftime("%Y-%m-%d %H:%M JST"),
         "items": uniq}, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(uniq)}件を {OUT.name} に書き出しました")
    if "--push" in sys.argv:
        subprocess.run(["git", "-C", str(BASE), "add", OUT.name], check=True)
        subprocess.run(["git", "-C", str(BASE), "commit", "-q", "-m",
                        f"ローカル取得ぶんを更新 {len(uniq)}件 "
                        f"({jst:%Y-%m-%d %H:%M})"], check=False)
        subprocess.run(["git", "-C", str(BASE), "pull", "--rebase", "-q",
                        "origin", "main"], check=False)
        subprocess.run(["git", "-C", str(BASE), "push", "-q",
                        "origin", "HEAD"], check=False)
        print("pushしました")


if __name__ == "__main__":
    main()
