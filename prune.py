#!/usr/bin/env python3
"""掲載終了だけを数分で外す早回し。全件取り直し(daily.yml)の代わりではない。

各物件のURLを開き、404か /library/ への転送を掲載終了とみなしてカードとJSONから消す。
判定できなかった物件(403・タイムアウト)は残す。1件も消さなければ何も書かない。
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests

FILES = ["index.html", "docs/index.html"]
DATA_RE = re.compile(r'(<script type="application/json" id="data">)(.*?)(</script>)', re.S)


def status(url):
    try:
        r = requests.get(url, impersonate="chrome", timeout=25, allow_redirects=True)
    except Exception:
        return "unknown"
    if r.status_code == 404 or "/library/" in str(r.url):
        return "ended"
    return "live" if r.status_code == 200 else "unknown"


def main():
    html = open(FILES[0], encoding="utf-8").read()
    m = DATA_RE.search(html)
    data = json.loads(m.group(2).replace("<\\/", "</"))
    with ThreadPoolExecutor(8) as ex:
        res = dict(zip(data, ex.map(lambda k: status(data[k]["url"]), data)))
    counts = {s: sum(v == s for v in res.values()) for s in ("live", "ended", "unknown")}
    print(counts)
    ended = [k for k, v in res.items() if v == "ended"]
    if not ended:
        return
    for k in ended:
        html = re.sub(r'<div class="card"\s+data-id="%s".*?(?=<div class="card"|</main>)' % re.escape(k), "", html, flags=re.S)
        del data[k]
    dump = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = DATA_RE.sub(lambda mm: mm.group(1) + dump + mm.group(3), html, count=1)
    html = re.sub(r"物件在庫 \d+件", "物件在庫 %d件" % len(data), html)
    for f in FILES:
        open(f, "w", encoding="utf-8").write(html)
    print("removed", len(ended), "->", len(data))


if __name__ == "__main__":
    sys.exit(main())
