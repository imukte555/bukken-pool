"""IPを変える／時間を空ける／別ドメインで越えられるか実測。"""
import time, os
from curl_cffi import requests as cr
import requests as rq

SLUG="oimachi_00603-st"
U=f"https://www.homes.co.jp/mansion/chuko/tokyo/{SLUG}/list/"

def ok(r): return r is not None and getattr(r,"status_code",0)==200 and len(r.text)>50000
def get(**kw):
    try: return cr.get(U, impersonate="chrome120", timeout=30, **kw)
    except Exception: return None

print("=== 0. 現在のIPと素の状態 ===", flush=True)
try: print("  IP:", rq.get("https://api.ipify.org", timeout=15).text, flush=True)
except Exception as e: print("  IP取得失敗", e, flush=True)
r=get(); print(f"  1回目: {'OK' if ok(r) else 'ブロック'}", flush=True)

print("\n=== 1. 使い切ってから何分で復帰するか ===", flush=True)
n=0
for i in range(10):
    if ok(get()): n+=1
    time.sleep(2)
print(f"  連続で {n}/10 成功（ここでブロック状態）", flush=True)
for wait in (60, 120, 300):
    time.sleep(wait)
    r=get()
    print(f"  {wait}秒待機後: {'復帰OK' if ok(r) else 'まだブロック'}", flush=True)
    if ok(r): break

print("\n=== 2. モバイル版/別ホストは別枠か ===", flush=True)
for label,u in [("m.homes.co.jp", U.replace("www.","m.")),
                ("末尾スラッシュ無し", U.rstrip("/")),
                ("?page=1付き", U+"?page=1")]:
    try:
        r=cr.get(u, impersonate="chrome120", timeout=30)
        print(f"  {label:<20} HTTP{r.status_code} len={len(r.text)} {'OK' if ok(r) else 'NG'}", flush=True)
    except Exception as e:
        print(f"  {label:<20} ERR {e}", flush=True)
    time.sleep(5)

print("\n=== 3. 公開プロキシ経由（r.jina.ai） ===", flush=True)
for i in range(3):
    try:
        r=rq.get("https://r.jina.ai/"+U, timeout=45)
        good = r.status_code==200 and len(r.text)>20000
        print(f"  {i+1}: HTTP{r.status_code} len={len(r.text)} {'OK' if good else 'NG'}", flush=True)
    except Exception as e:
        print(f"  {i+1}: ERR {str(e)[:60]}", flush=True)
    time.sleep(5)
