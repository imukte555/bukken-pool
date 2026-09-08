"""HOMESの6件制限を越える方法を実測で探す。"""
import time, random
from curl_cffi import requests as cr

SLUGS = ["oimachi_00603-st","ebisu_00577-st","meguro_00577-st","nakameguro_00577-st",
         "togoshi_06400-st","gotanda_00603-st","musashikoyama_05069-st","kamata_00605-st",
         "fudomae_05068-st","mita_06402-st","sengakuji_05181-st","hiro_06347-st"]
def url(i, kind="mansion"):
    s = SLUGS[i % len(SLUGS)]
    return {"mansion": f"https://www.homes.co.jp/mansion/chuko/tokyo/{s}/list/",
            "house":   f"https://www.homes.co.jp/kodate/chuko/tokyo/{s}/list/",
            "land":    f"https://www.homes.co.jp/tochi/tokyo/{s}/list/"}[kind]

def ok(r):
    return r is not None and r.status_code == 200 and len(r.text) > 50000

def run(label, fn, n=14):
    print(f"\n=== {label} ===", flush=True)
    got = 0
    for i in range(n):
        try:
            r = fn(i)
        except Exception:
            r = None
        good = ok(r)
        got += good
        print(f"  {i+1:>2}: {'OK' if good else 'ブロック'} "
              f"HTTP{r.status_code if r is not None else 'ERR'}", flush=True)
        if not good and i >= 7:
            break
        time.sleep(3)
    print(f"  → {got}/{n} 成功", flush=True)
    return got

# A: 毎回新しいSessionを作る
def a(i):
    s = cr.Session(impersonate="chrome120")
    return s.get(url(i), timeout=30)
run("A: 毎回新しいSession", a)
time.sleep(60)

# B: impersonate を毎回変える
IMPS = ["chrome120","chrome124","chrome131","safari17_0","edge101","chrome116","safari15_5"]
def b(i):
    return cr.get(url(i), impersonate=IMPS[i % len(IMPS)], timeout=30)
run("B: 指紋を毎回変える", b)
time.sleep(60)

# C: Referer を付けて自然な遷移に見せる
def c(i):
    h = {"Referer": "https://www.homes.co.jp/", "Accept-Language": "ja,en;q=0.9"}
    return cr.get(url(i), headers=h, impersonate="chrome120", timeout=30)
run("C: Referer付き", c)
time.sleep(60)

# D: 種別を変える（同一駅の別カテゴリ）
def d(i):
    return cr.get(url(i // 3, ["mansion","house","land"][i % 3]),
                  impersonate="chrome120", timeout=30)
run("D: 種別ローテ", d)
