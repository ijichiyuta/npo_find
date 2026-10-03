#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
npo_leads.py — NPO法人「ホームページ未登録」リード抽出ツール

内閣府 NPO法人ポータルサイトの公開データ(一括ダウンロード)から、
「活動中」で「ホームページが未登録(または SNS/無料ブログのみ)」の法人を抽出し、
営業リスト(Excelで開けるCSV)として出力する。

データ元:
  https://www.npo-homepage.go.jp/npoportal/download/all
  - gyousei_000.zip : 行政入力情報(全所轄庁)  … 法人名/所在地/代表者/活動分野/解散情報 等
  - houjin_000.zip  : 法人入力情報(全所轄庁)  … 電話/FAX/ホームページ/メール/決算 等
  2つのCSVは行順が完全一致しており、行番号で確実に結合できる。

使い方の例:
  python3 npo_leads.py                         # 全国のHP未登録・活動中を抽出
  python3 npo_leads.py --pref 東京都           # 所在地が「東京都」の法人だけ
  python3 npo_leads.py --pref 神奈川 --field 13 # 神奈川 かつ 活動分野=子どもの健全育成
  python3 npo_leads.py --area 横浜市           # 所轄庁が「横浜市」
  python3 npo_leads.py --stats                 # 抽出せず、所轄庁別・分野別の件数だけ表示
  python3 npo_leads.py --include-sns           # SNS/無料ブログのみの法人もリードに含める
  python3 npo_leads.py --pref 大阪 --limit 300 --out osaka.csv
"""
import argparse
import csv
import glob
import io
import os
import re
import sys
import urllib.request
import zipfile

BASE_URL = "https://www.npo-homepage.go.jp/npoportal/download/zip"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.join(HERE, "output")

# --- 特定非営利活動促進法 別表の20分野(列12〜31が各分野の○マーカーに対応) ---
ACTIVITY = {
    1: "保健・医療・福祉",
    2: "社会教育",
    3: "まちづくり",
    4: "観光の振興",
    5: "農山漁村・中山間地域",
    6: "学術・文化・芸術・スポーツ",
    7: "環境保全",
    8: "災害救援",
    9: "地域安全",
    10: "人権・平和",
    11: "国際協力",
    12: "男女共同参画",
    13: "子どもの健全育成",
    14: "情報化社会",
    15: "科学技術振興",
    16: "経済活動活性化",
    17: "職業能力開発・雇用",
    18: "消費者保護",
    19: "中間支援(団体連絡・助言・援助)",
    20: "条例指定",
}

# --- 行政入力情報(Administrative)の列インデックス ---
A_NAME, A_KANA, A_SOKATSU = 0, 1, 2
A_ADDR, A_ZIP = 4, 5
A_REP = 7
A_AUTH_DATE = 9
A_PURPOSE = 11
A_FIELD0 = 12          # 活動分野1 (…31が分野20)
A_DISSOLVE = 47        # 解散情報(非空=解散/清算)
A_DETAIL_URL = 48      # NPOポータル詳細ページURL
A_CORPNUM = 49         # 法人番号

# --- 法人入力情報(Corporate)の列インデックス ---
C_NAME = 0
C_TEL, C_FAX, C_HP, C_MAIL = 1, 2, 3, 4
C_STAFF, C_DESC = 5, 6
C_REVENUE_TOTAL = 10   # 経常収益計-合計(規模の目安)

# 政令指定都市(所轄庁)→ 都道府県 の対応。所在地に都道府県名が入らないため、
# 都道府県での絞り込みは所轄庁から導出する。
SEIREI_TO_PREF = {
    "札幌市": "北海道", "仙台市": "宮城県", "さいたま市": "埼玉県", "千葉市": "千葉県",
    "横浜市": "神奈川県", "川崎市": "神奈川県", "相模原市": "神奈川県",
    "新潟市": "新潟県", "静岡市": "静岡県", "浜松市": "静岡県", "名古屋市": "愛知県",
    "京都市": "京都府", "大阪市": "大阪府", "堺市": "大阪府", "神戸市": "兵庫県",
    "岡山市": "岡山県", "広島市": "広島県", "北九州市": "福岡県", "福岡市": "福岡県",
    "熊本市": "熊本県",
}


def pref_of(sokatsu):
    """所轄庁名から都道府県名を返す(政令市はその親県に変換)。"""
    s = (sokatsu or "").strip()
    return SEIREI_TO_PREF.get(s, s)


# SNS・無料ブログ・簡易ホスティング等の判定(独自サイトとは見なしにくいもの)
SNS_PATTERNS = re.compile(
    r"(facebook\.com|instagram\.com|twitter\.com|x\.com|ameblo\.jp|"
    r"ameba\.jp|blog\.|\.blog|jimdo|wixsite\.com|web\.fc2\.com|"
    r"hp\.infoseek|goo\.ne\.jp|jugem|seesaa|livedoor\.|note\.com|"
    r"peraichi\.com|localinfo\.jp|crayonsite|wordpress\.com|"
    r"blogspot|geocities|\.wixsite|hatenablog|fc2web\.com)",
    re.IGNORECASE,
)


def log(msg):
    print(msg, file=sys.stderr)


def download_if_needed():
    """行政/法人 CSVが無ければ zip をダウンロードして展開する。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    admin = _find_csv("*AdministrativeInputData*.csv")
    corp = _find_csv("*CorporateInputData*.csv")
    if admin and corp:
        return admin, corp
    for code, kind in [("gyousei_000", "行政入力"), ("houjin_000", "法人入力")]:
        zpath = os.path.join(DATA_DIR, code + ".zip")
        if not os.path.exists(zpath):
            url = f"{BASE_URL}/{code}.zip"
            log(f"ダウンロード中({kind}): {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as r, open(zpath, "wb") as f:
                f.write(r.read())
        with zipfile.ZipFile(zpath) as z:
            for n in z.namelist():
                if n.lower().endswith(".csv"):
                    z.extract(n, DATA_DIR)
    admin = _find_csv("*AdministrativeInputData*.csv")
    corp = _find_csv("*CorporateInputData*.csv")
    if not (admin and corp):
        log("エラー: CSVの取得に失敗しました。")
        sys.exit(1)
    return admin, corp


def _find_csv(pattern):
    hits = sorted(glob.glob(os.path.join(DATA_DIR, pattern)))
    return hits[-1] if hits else None


def load_csv(path):
    text = open(path, "rb").read().decode("cp932", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def classify_hp(url):
    """登録HP文字列を分類して返す: 'none' / 'sns' / 'site'"""
    u = (url or "").strip()
    if not u:
        return "none"
    if SNS_PATTERNS.search(u):
        return "sns"
    return "site"


def fields_of(admin_row):
    """行の活動分野○マーカーを短縮名リストに変換。"""
    out = []
    for i in range(20):
        if admin_row[A_FIELD0 + i].strip():
            out.append(ACTIVITY[i + 1])
    return out


def field_numbers_of(admin_row):
    return {i + 1 for i in range(20) if admin_row[A_FIELD0 + i].strip()}


def main():
    ap = argparse.ArgumentParser(
        description="NPO法人ポータルの公開データから『HP未登録・活動中』の営業リードを抽出する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--pref", default="", help="都道府県で絞る(例: 東京都, 神奈川, 大阪)。政令市も親県に含めて抽出")
    ap.add_argument("--area", default="", help="所轄庁名そのもので絞る(例: 横浜市だけ, 大阪府だけ)")
    ap.add_argument("--field", default="", help="活動分野で絞る。番号(1-20)またはキーワード(例: 福祉,子ども)。カンマ区切りでOR")
    ap.add_argument("--keyword", default="", help="法人名・事業目的に含まれるキーワードで絞る")
    ap.add_argument("--established-after", type=int, default=0, help="設立認証年がこの年以降のみ(例: 2015)")
    ap.add_argument("--include-sns", action="store_true", help="登録HPがSNS/無料ブログのみの法人もリードに含める")
    ap.add_argument("--include-registered", action="store_true", help="HP登録済みの法人も含める(通常は除外)")
    ap.add_argument("--limit", type=int, default=0, help="出力件数の上限(0=無制限)")
    ap.add_argument("--stats", action="store_true", help="抽出せず、所轄庁別・活動分野別の件数を表示")
    ap.add_argument("--out", default="", help="出力CSVパス(既定: output/leads_<条件>.csv)")
    args = ap.parse_args()

    admin_path, corp_path = download_if_needed()
    log(f"読込: {os.path.basename(admin_path)} / {os.path.basename(corp_path)}")
    admin = load_csv(admin_path)[1:]
    corp = load_csv(corp_path)[1:]
    n = min(len(admin), len(corp))
    log(f"総法人数: {n}")

    # --field 指定の解釈(番号 or キーワード)
    want_fields = set()
    if args.field:
        for tok in args.field.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok.isdigit():
                want_fields.add(int(tok))
            else:
                for num, name in ACTIVITY.items():
                    if tok in name:
                        want_fields.add(num)

    if args.stats:
        print_stats(admin, n)
        return

    rows = []
    for i in range(n):
        a = admin[i]
        c = corp[i]
        # 1) 活動中のみ(解散/清算を除外)
        if a[A_DISSOLVE].strip():
            continue
        # 2) HP判定
        hp_kind = classify_hp(c[C_HP])
        if not args.include_registered:
            if hp_kind == "site":
                continue
            if hp_kind == "sns" and not args.include_sns:
                continue
        # 3) 都道府県(所轄庁から導出)/所轄庁フィルタ
        if args.pref and args.pref not in pref_of(a[A_SOKATSU]):
            continue
        if args.area and args.area not in a[A_SOKATSU]:
            continue
        # 4) 活動分野フィルタ
        if want_fields and not (want_fields & field_numbers_of(a)):
            continue
        # 5) キーワード
        if args.keyword and (args.keyword not in a[A_NAME] and args.keyword not in a[A_PURPOSE]):
            continue
        # 6) 設立年
        if args.established_after:
            m = re.search(r"(\d{4})", a[A_AUTH_DATE])
            if not m or int(m.group(1)) < args.established_after:
                continue

        rows.append({
            "法人番号": a[A_CORPNUM].strip(),
            "法人名称": a[A_NAME].strip(),
            "法人名称カナ": a[A_KANA].strip(),
            "都道府県": pref_of(a[A_SOKATSU]),
            "郵便番号": fmt_zip(a[A_ZIP]),
            "所在地": a[A_ADDR].strip(),
            "代表者氏名": a[A_REP].strip(),
            "電話番号": c[C_TEL].strip(),
            "FAX": c[C_FAX].strip(),
            "メールアドレス": c[C_MAIL].strip(),
            "活動分野": " / ".join(fields_of(a)),
            "事業目的": a[A_PURPOSE].strip()[:120],
            "事業活動の内容": c[C_DESC].strip()[:120],
            "設立認証年月日": a[A_AUTH_DATE].strip(),
            "直近経常収益": c[C_REVENUE_TOTAL].strip(),
            "所轄庁": a[A_SOKATSU].strip(),
            "登録HP種別": {"none": "未登録", "sns": "SNS/ブログのみ", "site": "登録あり"}[hp_kind],
            "登録HP": c[C_HP].strip(),
            "NPOポータル詳細URL": a[A_DETAIL_URL].strip(),
            "Web確認結果": "",   # verify_web.py で埋める列
        })

    if args.limit and len(rows) > args.limit:
        log(f"上限 {args.limit} 件に切り詰め(該当は {len(rows)} 件)")
        rows = rows[: args.limit]

    out = args.out or default_outname(args)
    os.makedirs(OUT_DIR, exist_ok=True)
    if not os.path.isabs(out):
        out = os.path.join(OUT_DIR, out)
    write_csv(out, rows)
    log(f"\n✅ {len(rows)} 件を出力しました → {out}")
    if rows:
        log("先頭サンプル:")
        for r in rows[:5]:
            log(f"  ・{r['法人名称']}（{r['所在地'][:18]}）{r['活動分野']}")


def fmt_zip(z):
    z = re.sub(r"\D", "", z or "")
    return f"{z[:3]}-{z[3:]}" if len(z) == 7 else z


def default_outname(args):
    parts = []
    if args.pref: parts.append(args.pref)
    if args.area: parts.append(args.area)
    if args.field: parts.append("分野" + args.field.replace(",", "-"))
    if args.keyword: parts.append(args.keyword)
    tag = "_".join(parts) if parts else "全国"
    if args.include_sns: tag += "_SNS込"
    return f"leads_{tag}.csv"


def write_csv(path, rows):
    cols = ["法人番号", "法人名称", "法人名称カナ", "都道府県", "郵便番号", "所在地", "代表者氏名",
            "電話番号", "FAX", "メールアドレス", "活動分野", "事業目的", "事業活動の内容",
            "設立認証年月日", "直近経常収益", "所轄庁", "登録HP種別", "登録HP",
            "NPOポータル詳細URL", "Web確認結果"]
    # Excelで文字化けしないよう UTF-8 BOM 付き
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def print_stats(admin, n):
    from collections import Counter
    sok = Counter()
    fld = Counter()
    active = 0
    for a in admin[:n]:
        if a[A_DISSOLVE].strip():
            continue
        active += 1
        sok[a[A_SOKATSU].strip()] += 1
        for num in field_numbers_of(a):
            fld[ACTIVITY[num]] += 1
    print(f"活動中NPO: {active} 件\n")
    print("=== 所轄庁別(トップ25) ===")
    for k, v in sok.most_common(25):
        print(f"  {v:6d}  {k}")
    print("\n=== 活動分野別(のべ) ===")
    for name in ACTIVITY.values():
        if fld.get(name):
            print(f"  {fld[name]:6d}  {name}")


if __name__ == "__main__":
    main()
