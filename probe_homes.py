"""ActionsのIPからHOMESを何件取れるか、間隔を変えて実測する。"""
import time, re
from curl_cffi import requests as cr

ST = [("大井町","oimachi_00603-st"),("恵比寿","ebisu_00577-st"),("目黒","meguro_00577-st"),
      ("中目黒","nakameguro_00577-st"),("戸越","togoshi_06400-st"),("五反田","gotanda_00603-st")]
KINDS = [("mansion","mansion/chuko/tokyo/{}/list/"),
         ("house","kodate/chuko/tokyo/{}/list/"),
         ("land","tochi/tokyo/{}/list/")]

for interval in (3, 6, 10):
    print(f"\n=== 間隔{interval}秒 ===", flush=True)
    ok = ng = 0
    for st, slug in ST:
        for kind, path in KINDS:
            u = f"https://www.homes.co.jp/{path.format(slug)}"
            try:
                r = cr.get(u, impersonate="chrome120", timeout=30)
                good = r.status_code == 200 and len(r.text) > 50000
            except Exception:
                good = False
                r = None
            code = r.status_code if r is not None else "ERR"
            n = len(r.text) if r is not None else 0
            print(f"  {st:<6}{kind:<8} HTTP{code} len={n:<7}{'OK' if good else 'ブロック'}", flush=True)
            ok += good; ng += (not good)
            time.sleep(interval)
    print(f"  → 成功 {ok} / 失敗 {ng}", flush=True)
    if ng == 0:
        print(f"  間隔{interval}秒なら全部通る", flush=True)
        break
    time.sleep(90)
