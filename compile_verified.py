#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compile_verified.py — Claudeが手検証した結果(data/claude_verdicts.json)を
リードCSVに突き合わせ、営業用の検証済みリストを出力する。

verdict: lead=独自サイト無し(有力) / weak=簡易・旧式・SNSのみ(準リード) / has=独自サイトあり(除外)
JSONのキーは法人名に含まれる一意な部分文字列。値は [verdict, 参考URL, 電話].

使い方:
  python3 compile_verified.py output/aichi_new.csv output/leads_aichi.csv
  → output/aichi_verified_sample.csv を生成
"""
import csv, json, os, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
VERDICT_PATH = os.path.join(HERE, "data", "claude_verdicts.json")
LABEL = {"lead": "見つからず(有力リード)", "weak": "簡易/旧式/SNSのみ", "has": "独自サイトあり(除外)"}
RANK = {"lead": 0, "weak": 1, "has": 2}
COLS = ["Web確認結果", "法人名称", "都道府県", "所在地", "代表者氏名", "電話番号",
        "活動分野", "設立認証年月日", "事業目的", "Web確認_参考URL", "NPOポータル詳細URL"]


def main():
    srcs = sys.argv[1:] or ["output/aichi_new.csv", "output/leads_aichi.csv"]
    verdicts = json.load(open(VERDICT_PATH, encoding="utf-8"))
    seen = {}
    for path in srcs:
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            seen.setdefault(r["法人名称"], r)

    def match(name):
        for key, val in verdicts.items():
            if key in name:
                return val
        return None

    out = []
    for name, r in seen.items():
        m = match(name)
        if not m:
            continue
        v, ev, tel = m
        r = dict(r)
        r["Web確認結果"] = LABEL[v]
        r["Web確認_参考URL"] = ev
        if tel and not r.get("電話番号"):
            r["電話番号"] = tel + "(検索取得)"
        r["_rank"] = RANK[v]
        out.append(r)
    out.sort(key=lambda x: (x["_rank"], x["設立認証年月日"]))

    outpath = os.path.join(HERE, "output", "aichi_verified_sample.csv")
    with open(outpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in out:
            w.writerow(r)

    c = Counter(r["Web確認結果"] for r in out)
    print(f"検証済み {len(out)} 件:")
    for k, v in sorted(c.items()):
        print(f"  {v:3d}  {k}")
    leads = [r for r in out if r["Web確認結果"].startswith("見つからず")]
    print(f"\n★有力リード {len(leads)} 件:")
    for r in leads:
        nm = r["法人名称"].replace("特定非営利活動法人　", "").replace("特定非営利活動法人", "")
        print(f"  ★ {nm[:16]:　<16} | {r['所在地'][:20]:　<20} | {r['設立認証年月日']} | {r['電話番号'] or '電話不明'}")
    print(f"\n出力: {outpath}")


if __name__ == "__main__":
    main()
