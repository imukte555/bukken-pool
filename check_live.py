#!/usr/bin/env python3
"""公開ページを「実際のブラウザで見えているか」まで確かめる。

HTTPで200が返ることと、ブラウザで表示できることは別物だった。
実測: goo住宅の中継画像 img.house.goo.ne.jp は curl では200を返すのに
<img> では読めず、代表画像138枚のうち92枚が真っ白だった。
サーバ側のチェックだけでは絶対に気づけないので、公開後に必ず
Edgeでこのスクリプトが出すJSを実行して確かめる。

  python3 check_live.py          # 実行するJSを出力する
  python3 check_live.py --http   # HTTPで分かる範囲だけ先に見る
"""
import re
import sys
import urllib.request

URL = "https://imukte555.github.io/bukken-pool/"

JS = r"""
if(!/Edg\//.test(navigator.userAgent)) throw new Error('Edgeで開くこと');
document.querySelectorAll('img').forEach(i=>i.loading='eager');
await new Promise(r=>setTimeout(r,9000));
const rep=[...document.querySelectorAll('.thumb img')];
const sub=[...document.querySelectorAll('.sub-th img')];
const dead=a=>a.filter(i=>!i.complete||i.naturalWidth===0);
const host=u=>{try{return new URL(u).host}catch(e){return '?'}};
const bh={};
dead(rep.concat(sub)).forEach(i=>{bh[host(i.src)]=(bh[host(i.src)]||0)+1});
JSON.stringify({
  代表:rep.length, 代表表示:rep.length-dead(rep).length,
  子行:sub.length, 子行表示:sub.length-dead(sub).length,
  壊れホスト:bh,
  判定:(dead(rep).length===0&&dead(sub).length===0)?'OK':'NG'
})
"""


def http_check():
    with urllib.request.urlopen(URL + "?cb=check") as r:
        h = r.read().decode("utf-8")
    body = h.split('<details class="rej"')[0]
    cards = re.split(r'(?=<div class="card")', body)[1:]
    srcs = re.findall(r"<img[^>]+src='([^']+)'", body)
    hosts = {}
    for u in srcs:
        m = re.match(r"https?://([^/]+)", u)
        if m:
            hosts[m.group(1)] = hosts.get(m.group(1), 0) + 1
    print(f"カード {len(cards)}件 / 画像 {len(srcs)}件")
    print("画像ホスト:", dict(sorted(hosts.items(), key=lambda kv: -kv[1])))
    # 中継ホストが残っていたらブラウザで表示できない
    bad = [u for u in srcs
           if re.search(r"img\.house\.goo\.ne\.jp/|img01\.suumo\.com/jj/", u)]
    if bad:
        print(f"NG: ブラウザで表示できない中継URLが {len(bad)}件 残っている")
        return 1
    print("OK: 中継URLは残っていない（ただし表示確認はブラウザで行うこと）")
    return 0


if __name__ == "__main__":
    if "--http" in sys.argv:
        sys.exit(http_check())
    print("Edgeで公開ページを開き、次のJSを実行して 判定=OK を確認すること:")
    print(f"  URL: {URL}?cb=<乱数>")
    print(JS)
