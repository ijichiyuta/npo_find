#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_web.py — 「検索しても出てこない」NPOを自動判定する(Stage 2)

npo_leads.py が出力したリード CSV を入力に、各法人名を Web 検索して
「独自サイトあり / SNS・ブログのみ / 見つからない」を判定し、
CSV の「Web確認結果」列に書き込む。

  ・独自サイトが無い/検索で出てこない法人 = 最有力の営業先
  ・SNS のみの法人 = きちんとしたHPが無いので営業余地あり

検索エンジンは Google Programmable Search (Custom Search JSON API) を使用。
  無料枠 100 クエリ/日、以降 $5/1000件(最大1万件/日)。
  1) https://programmablesearchengine.google.com/ で検索エンジンを作成
     (「ウェブ全体を検索」をON) → 検索エンジンID(cx) を取得
  2) https://developers.google.com/custom-search/v1/introduction で APIキーを取得
  3) 環境変数をセット:
       export GOOGLE_API_KEY="＜APIキー＞"
       export GOOGLE_CSE_ID="＜検索エンジンID＞"

  ※APIキーを用意できない場合は、Claude(このツール)に
    「このCSVをWeb検証して」と依頼すれば、Claudeが検索して判定できます。

結果は data/web_cache.json にキャッシュし、再実行時は再検索しない。

使い方:
  python3 verify_web.py output/leads_神奈川_分野子ども.csv --limit 100
  python3 verify_web.py output/leads_東京都.csv --limit 200
  # 「見つからない」だけを別ファイルに:  --only-missing
"""
import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "data", "web_cache.json")

# 検索結果に出ても「その法人の独自サイト」とは見なさない集約/名鑑/行政系ドメイン
AGGREGATOR = [
    "npo-homepage.go.jp", "houjin-bangou.nta.go.jp", "houjin.jp", "houjinbangou",
    "gbiz-info.go.jp", "info.gbiz.go.jp", "baseconnect.in", "musubu.in",
    "alarmbox.jp", "jpnumber.com", "navitime.co.jp", "mapion.co.jp",
    "itp.ne.jp", "ipros.jp", "salesnow.jp", "prtimes.jp", "wantedly.com",
    "en-hyouban.com", "indeed.com", "townwork.net", "hellowork",
    "jobtag", "mapfan.com", "goo.ne.jp/map", "google.com/maps",
    "npo-hiroba.or.jp", "canpan.info", "activo.jp", "yolo-japan",
    "job-medley.com", "kaigo", "guidebook", "ekiten.jp", "loco.yahoo",
    "credit.co.jp", "nikkei.com", "atengineer",
    # NPO名鑑・データベース・市民活動センター等(独自サイトではない)
    "npodb.info", "kana-con.net", "f-npon.jp", "kyuminyokin.info",
    "shingaku-fs.jp", "miraitizu.com", "yahoo.co.jp", "mynavi",
    "rikunabi", "hellowork", "npokwsk", "volunteer", "compass-npo",
    ".lg.jp", "pref.", ".city.", ".town.", ".go.jp", "e-gov",
    "facebook.com/marketplace", "tabelog", "retty", "hotpepper",
]

# SNS・無料ブログ・簡易サイト(独自の本格サイトとは言い難い)
SNS = [
    "facebook.com", "instagram.com", "twitter.com", "x.com", "ameblo.jp",
    "ameba.jp", "jimdo", "wixsite.com", "web.fc2.com", "fc2web.com",
    "hp.infoseek", "jugem", "seesaa", "livedoor", "note.com",
    "peraichi.com", "crayonsite", "wordpress.com", "blogspot",
    "hatenablog", "hatena.ne.jp", "localinfo.jp", "youtube.com",
    "line.me", "goo.gl", "linktr.ee",
]


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            return json.load(open(CACHE_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    json.dump(cache, open(CACHE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")


def google_search(query, timeout=15):
    """Google Custom Search JSON API で検索し、(タイトル, URL) のリストを返す。"""
    params = urllib.parse.urlencode({
        "key": GOOGLE_API_KEY, "cx": GOOGLE_CSE_ID,
        "q": query, "num": 10, "hl": "ja", "gl": "jp",
    })
    url = "https://www.googleapis.com/customsearch/v1?" + params
    body = urllib.request.urlopen(url, timeout=timeout).read().decode("utf-8", "replace")
    data = json.loads(body)
    return [(it.get("title", ""), it.get("link", "")) for it in data.get("items", [])]


def search(query, timeout=15):
    if not (GOOGLE_API_KEY and GOOGLE_CSE_ID):
        raise RuntimeError(
            "環境変数 GOOGLE_API_KEY と GOOGLE_CSE_ID が未設定です。\n"
            "  Google Custom Search のキーを設定するか、Claudeに検証を依頼してください。\n"
            "  (詳細はこのファイル冒頭のコメント参照)")
    return google_search(query, timeout)


def domain_of(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def is_aggregator(url):
    u = url.lower()
    return any(a in u for a in AGGREGATOR)


def is_sns(url):
    u = url.lower()
    return any(s in u for s in SNS)


def normalize_name(name):
    """検索用に法人名を整える。法人格表記の揺れを吸収したコア名も作る。"""
    n = name.replace("　", " ").strip()
    core = re.sub(r"(特定非営利活動法人|認定特定非営利活動法人|ＮＰＯ法人|NPO法人|NPO)", "", n)
    core = core.replace("（", "").replace("）", "").replace("(", "").replace(")", "").strip()
    return n, core


def classify(name, area, results):
    """検索結果からサイト有無を判定する。"""
    _, core = normalize_name(name)
    core_key = re.sub(r"\s+", "", core)
    site_hits, sns_hits = [], []
    for title, url in results[:12]:
        if not url.startswith("http"):
            continue
        if is_aggregator(url):
            continue
        t = re.sub(r"\s+", "", title)
        # タイトルに法人コア名の一部が含まれていれば、その法人関連ページと判断
        related = bool(core_key) and (core_key[:4] in t or core_key[:6] in title or core_key in title)
        if is_sns(url):
            if related:
                sns_hits.append(url)
            continue
        if related:
            site_hits.append(url)
    if site_hits:
        return "独自サイトあり", site_hits[0]
    if sns_hits:
        return "SNS・ブログのみ", sns_hits[0]
    return "見つからず(有力リード)", ""


def main():
    ap = argparse.ArgumentParser(description="リードCSVの各法人をWeb検索し、サイト有無を判定")
    ap.add_argument("input", help="npo_leads.py が出力したCSV")
    ap.add_argument("--limit", type=int, default=100, help="検索する件数上限(既定100)")
    ap.add_argument("--sleep", type=float, default=2.0, help="1件ごとの待機秒(既定2.0)")
    ap.add_argument("--out", default="", help="出力CSV(既定: 入力名_verified.csv)")
    ap.add_argument("--only-missing", action="store_true", help="『見つからず』の行だけを出力")
    ap.add_argument("--redo", action="store_true", help="既に確認済みの行も再検索する")
    args = ap.parse_args()

    if not (GOOGLE_API_KEY and GOOGLE_CSE_ID):
        print(
            "\n[停止] Web検索の認証情報がありません。\n"
            "  方法A) Google Custom Search のキーを設定して再実行:\n"
            "     export GOOGLE_API_KEY=\"...\"\n"
            "     export GOOGLE_CSE_ID=\"...\"\n"
            "  方法B) Claude(このツール)に『このCSVをWeb検証して』と依頼する。\n"
            "  詳細は verify_web.py 冒頭のコメントを参照。\n", file=sys.stderr)
        sys.exit(2)

    rows = list(csv.DictReader(open(args.input, encoding="utf-8-sig")))
    if not rows:
        print("入力に行がありません。", file=sys.stderr); sys.exit(1)
    cache = load_cache()

    checked = 0
    for r in rows:
        if checked >= args.limit:
            break
        if r.get("Web確認結果") and not args.redo:
            continue
        name = r["法人名称"]
        area = r.get("都道府県", "") or r.get("所轄庁", "")
        _, core = normalize_name(name)
        query = f"{core} {area}".strip()
        ckey = query
        if ckey in cache and not args.redo:
            verdict, evidence = cache[ckey]["verdict"], cache[ckey]["evidence"]
        else:
            try:
                res = search(query)
                verdict, evidence = classify(name, area, res)
                cache[ckey] = {"verdict": verdict, "evidence": evidence}
                save_cache(cache)
                time.sleep(args.sleep)
            except Exception as e:
                verdict, evidence = f"検索エラー: {type(e).__name__}", ""
                time.sleep(args.sleep)
        r["Web確認結果"] = verdict
        r["Web確認_参考URL"] = evidence
        checked += 1
        mark = {"独自サイトあり": "  ", "SNS・ブログのみ": "△ ", "見つからず(有力リード)": "★ "}.get(verdict, "? ")
        print(f"{mark}[{checked:4d}] {name[:28]:　<28} → {verdict} {evidence[:50]}", file=sys.stderr)

    out = args.out or re.sub(r"\.csv$", "_verified.csv", args.input)
    outrows = rows
    if args.only_missing:
        outrows = [r for r in rows if r.get("Web確認結果", "").startswith("見つからず")]
    cols = list(rows[0].keys())
    if "Web確認_参考URL" not in cols:
        cols.append("Web確認_参考URL")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in outrows:
            w.writerow({k: r.get(k, "") for k in cols})

    from collections import Counter
    c = Counter(r.get("Web確認結果", "") for r in rows if r.get("Web確認結果"))
    print("\n=== 判定サマリ ===", file=sys.stderr)
    for k, v in c.most_common():
        print(f"  {v:5d}  {k}", file=sys.stderr)
    print(f"\n✅ 出力: {out}（{len(outrows)}件）", file=sys.stderr)


if __name__ == "__main__":
    main()
