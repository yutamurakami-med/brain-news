"""ニュース候補を集めて、Claude で選定・要約・コメント下書きを作り、posts/ に記事ファイルを書き出す。

GitHub Actions（.github/workflows/collect.yml）から 1日3回 実行される。
必要な環境変数: ANTHROPIC_API_KEY（GitHub の Secrets に登録）
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from postfmt import CATEGORIES, parse_post, render_post  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "posts"
DRAFTS = ROOT / "drafts"          # AIが最初に書いたコメント（Fumiさんの修正前）を保存
STYLE = ROOT / "style" / "comment-style.md"
SELECTION = ROOT / "style" / "selection.md"
JST = timezone(timedelta(hours=9))
NOW = datetime.now(JST)

# ============================================================
# ここから下は「設定」。キーワードや本数を変えたいときはここを編集
# ============================================================
PICK_COUNT = 5          # 1回の配信で選ぶ本数
LOOKBACK_HOURS = 36     # 何時間前までのニュースを候補にするか
MODEL = "claude-sonnet-5-5"

# PR TIMES は全業種のリリースが流れてくるので、まずキーワードで絞る
JP_KEYWORDS = [
    "脳", "認知症", "アルツハイマー", "脳卒中", "脳梗塞", "神経", "頭痛", "片頭痛", "不眠", "てんかん", "パーキンソン",
    "抗加齢", "アンチエイジング", "老化", "長寿", "ロンジェビティ", "健康寿命", "フレイル",
    "予防", "検診", "健診", "人間ドック", "ワクチン", "生活習慣病", "睡眠", "健康経営",
    "医師", "医療", "クリニック", "病院", "診療", "製薬", "創薬", "治験", "臨床試験",
    "厚生労働", "オンライン診療", "ヘルスケア", "大学", "研究",
]

RSS_FEEDS = [
    {"name": "PR TIMES", "url": "https://prtimes.jp/index.rdf", "keyword_filter": True},
    {"name": "厚生労働省", "url": "https://www.mhlw.go.jp/stf/news.rdf", "keyword_filter": True, "fetch_body": True},
]
MAX_PAPERS = 2          # 1回の配信に入れる PubMed 論文の上限
EXAMPLE_COUNT = 8       # お手本として見せる、公開済みコメントの数

# PubMed：主要誌の新着論文に絞る
PUBMED_QUERY = (
    '("N Engl J Med"[ta] OR "Lancet"[ta] OR "JAMA"[ta] OR "BMJ"[ta] OR "Nat Med"[ta] '
    'OR "Lancet Neurol"[ta] OR "JAMA Neurol"[ta] OR "Neurology"[ta] OR "Stroke"[ta] '
    'OR "Alzheimers Dement"[ta] OR "Nat Aging"[ta] OR "J Neurosurg"[ta] OR "Lancet Healthy Longev"[ta]) '
    'AND (brain OR stroke OR dementia OR alzheimer* OR neurolog* OR neurosurg* OR aging OR longevity '
    'OR prevention OR screening OR lifestyle OR sleep)'
)
# ============================================================

UA = {"User-Agent": "brain-news-bot/1.0 (+https://github.com)"}
feed_status = []  # PR の説明欄に出す「情報源ごとの取得結果」


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            d = datetime.strptime(s.replace("Z", "+00:00") if "T" in s else s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_feed(xml_bytes):
    """RSS 1.0 / 2.0 / Atom をざっくり読む。namespace は無視する。"""
    root = ET.fromstring(xml_bytes)
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        rec = {}
        for c in el:
            ctag = c.tag.split("}")[-1]
            if ctag == "link" and c.get("href"):
                rec["link"] = c.get("href")
            elif c.text and ctag not in rec:
                rec[ctag] = c.text.strip()
        items.append({
            "title": strip_tags(rec.get("title", "")),
            "url": rec.get("link") or el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""),
            "summary": strip_tags(rec.get("description") or rec.get("summary") or rec.get("encoded", ""))[:600],
            "date": parse_date(rec.get("date") or rec.get("pubDate") or rec.get("updated") or rec.get("published")),
            "org": rec.get("corp", ""),
        })
    return items


def page_text(url, limit=2500):
    """概要がないRSS（厚労省など）のために、リンク先ページの本文をざっくり取り出す。"""
    html = http_get(url).decode("utf-8", "ignore")
    html = re.sub(r"(?is)<(script|style|noscript|header|footer|nav)[^>]*>.*?</\1>", " ", html)
    m = re.search(r'(?is)<(main|article)[^>]*>(.*)</\1>', html) or re.search(r'(?is)id="(?:content|contents|main)[^"]*"[^>]*>(.*)', html)
    body = m.group(m.lastindex) if m else html
    return strip_tags(body)[:limit]


def collect_rss():
    out = []
    since = NOW - timedelta(hours=LOOKBACK_HOURS)
    for f in RSS_FEEDS:
        try:
            items = parse_feed(http_get(f["url"]))
        except Exception as e:  # noqa: BLE001
            feed_status.append(f"- {f['name']}: 取得失敗（{type(e).__name__}）")
            continue
        kept = 0
        for it in items:
            if it["date"] and it["date"] < since:
                continue
            if f["keyword_filter"] and not any(k in it["title"] + it["summary"] for k in JP_KEYWORDS):
                continue
            if f.get("fetch_body") and not it["summary"] and it["url"]:
                try:
                    it["summary"] = page_text(it["url"])
                except Exception:  # noqa: BLE001
                    pass
            src = f["name"] + (f"（{it['org']}）" if it["org"] else "")
            out.append({**it, "source": src})
            kept += 1
        feed_status.append(f"- {f['name']}: {len(items)}件中 {kept}件を候補に")
    return out


def collect_pubmed():
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    try:
        q = urllib.parse.urlencode({"db": "pubmed", "term": PUBMED_QUERY, "reldate": 3, "datetype": "edat",
                                    "retmax": 30, "retmode": "json", "sort": "pub_date"})
        ids = json.loads(http_get(base + "esearch.fcgi?" + q))["esearchresult"]["idlist"]
        if not ids:
            feed_status.append("- PubMed: 新着 0件")
            return []
        x = ET.fromstring(http_get(base + "efetch.fcgi?" + urllib.parse.urlencode(
            {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"})))
    except Exception as e:  # noqa: BLE001
        feed_status.append(f"- PubMed: 取得失敗（{type(e).__name__}）")
        return []
    out = []
    for art in x.iter("PubmedArticle"):
        pmid = art.findtext(".//PMID")
        title = "".join(art.find(".//ArticleTitle").itertext()) if art.find(".//ArticleTitle") is not None else ""
        abstract = " ".join("".join(a.itertext()) for a in art.iter("AbstractText"))
        journal = art.findtext(".//ISOAbbreviation") or art.findtext(".//Journal/Title") or ""
        types = [t.text for t in art.iter("PublicationType")]
        if not abstract or any(t in ("Comment", "Editorial", "Letter", "News", "Review", "Systematic Review",
                                      "Practice Guideline", "Guideline") for t in types):
            continue
        out.append({"title": title, "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "summary": abstract[:4000], "source": f"PubMed（{journal}）", "date": None, "org": ""})
    feed_status.append(f"- PubMed: {len(ids)}件中 {len(out)}件を候補に")
    return out


def known_urls():
    """公開済み・承認待ちの記事のURL（同じ記事を何度も出さないため）。"""
    texts = [p.read_text(encoding="utf-8") for p in POSTS.glob("*.md")]
    try:  # まだマージされていない候補ブランチも見る
        refs = subprocess.run(["git", "for-each-ref", "--format=%(refname)", "refs/remotes/origin/candidates"],
                              cwd=ROOT, capture_output=True, text=True).stdout.split()
        for ref in refs:
            names = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref, "posts"],
                                   cwd=ROOT, capture_output=True, text=True).stdout.split()
            for n in names:
                texts.append(subprocess.run(["git", "show", f"{ref}:{n}"], cwd=ROOT,
                                            capture_output=True, text=True).stdout)
    except Exception:  # noqa: BLE001
        pass
    urls = set()
    for t in texts:
        m = re.search(r"^URL[:：]\s*(\S+)", t, re.M)
        if m:
            urls.add(m.group(1))
    return urls


PROMPT = """あなたは、医師「村上友太」（脳神経外科専門医・抗加齢医学専門医・病理専門医・健康経営専門医）が運営するニュースアプリの編集アシスタントです。
一番の読者は、日本の健康意識の高い一般の人です（医療者も読みます）。

下の候補リストから、読者にとって価値の高いものを最大{n}本選んでください。
「記事選びの好み」に書かれた村上の好みを最優先にしてください。

## カテゴリ（cat に入れるID）
主題がいちばん近いものを1つ選ぶ。頭痛・睡眠・認知症が主題なら、脳機能ではなくそちらを優先する。
- brain（脳機能）: 脳の働きやしくみ、脳卒中、てんかん、パーキンソン病、脳の健康全般
- headache（頭痛）: 片頭痛、緊張型頭痛、頭痛の治療や予防
- sleep（睡眠）: 睡眠の質・時間・リズム、不眠や睡眠障害、睡眠と健康の関係
- dementia（認知症）: 認知症・アルツハイマー病、認知機能の低下、バイオマーカー、認知症の予防
- antiaging（アンチエイジング）: 老化のしくみ（エピゲノム、ミトコンドリア、細胞老化など）、健康寿命、フレイル、老化を遅らせる介入
- prev（予防医療）: 検診・健診、ワクチン、生活習慣病、運動・栄養、健康経営
- 医療制度・政策（地域医療構想、診療報酬など）や医師の研修・キャリアの話は、どのカテゴリにも入れず選ばない

## 選ぶ基準
- 新しい事実（研究結果、承認、制度変更、新サービス開始など）があるものを優先
- 日本の読者に関係の深いものを優先する。海外の制度や地域限定の話は、日本への示唆がはっきりある場合だけ
- PubMed の論文は最大{max_papers}本まで。PR TIMES・厚生労働省の候補に適したものがあれば必ず入れる
- 根拠の乏しい健康食品・サプリの宣伝、イベント告知だけのもの、医療と関係の薄いものは選ばない
- できるだけカテゴリが偏らないように
- 過去に村上が「載せない」と判断した記事（下に一覧、かっこ内は理由）と似たものは避ける
- 好みに合う記事が少ないときは、無理に{n}本そろえず、少なくてよい
- 概要が「（なし）」や途中で切れていて、内容（結果や具体策）を確かめられない候補は選ばない

## 各記事について作るもの
- title: 日本語の見出し（40字以内）。元の見出しの直訳ではなく、一般の人にも内容がひと目で分かる自分の言葉で。誇張しない
- points: 要点を3つ（各60字以内）。元の文章を書き写さず、必ず自分の言葉で要約する。研究なら対象・規模・結果・限界のどれかに触れる。数値は原文どおり正確に。
  専門用語はなるべく言い換え、使うときはかっこで短く説明する（例：p-tau217（認知症に関わるたんぱく質の血液指標））。
  略語は初めて出るときに正式名称を添える（例：MOG抗体関連疾患（MOGAD））。
  「未確認」「抄録が途中まで」「見出しのみ」など、資料が足りないことを読者向けの文章に書かない。書けない記事は選ばない
- comment: 「医師のひとこと」の下書き（120字前後）。下の「書き方の指示書」と「村上の修正例・公開済みコメント」に口調を合わせる。
  5本のコメントが同じ型にならないよう、切り口を変える。効果の断定・特定商品の推奨・診断や治療の指示に読める表現は避ける
- cat: カテゴリID

出力は次の形のJSONだけにしてください（説明文は不要）:
{{"picks": [{{"index": 候補番号, "cat": "sleep", "title": "...", "points": ["...", "...", "..."], "comment": "..."}}]}}

---
# 記事選びの好み（村上が編集しているファイル）
{selection}

---
# コメントの書き方の指示書（村上が編集しているファイル）
{style}

---
# 村上の修正例・公開済みコメント（新しい順）
{examples}

---
# 村上が「載せない」と判断した最近の記事
{rejected}

---
# 候補リスト
{items}
"""


def load_examples():
    """公開済み記事のコメント（=村上の完成版）と、載せなかった記事の見出しを集める。"""
    approved, rejected = [], []
    for f in sorted(POSTS.glob("*.md"), reverse=True):
        try:
            p = parse_post(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if p["publish"] and p["m"] and len(approved) < EXAMPLE_COUNT:
            d = DRAFTS / f"{f.stem}.txt"
            draft = d.read_text(encoding="utf-8").strip() if d.exists() else ""
            approved.append((p, draft))
        elif not p["publish"] and len(rejected) < 20:
            rejected.append(p["t"] + (f"（{p['reason']}）" if p["reason"] else ""))
    lines = []
    for i, (p, draft) in enumerate(approved, 1):
        lines.append(f"例{i}（{CATEGORIES[p['cat']]}）{p['t']}")
        if draft and draft != p["m"]:
            lines.append(f"  AIの下書き: {draft}")
            lines.append(f"  村上の修正後: {p['m']}")
        else:
            lines.append(f"  村上のコメント: {p['m']}")
    return ("\n".join(lines) or "（まだありません）"), ("\n".join(f"- {t}" for t in rejected) or "（まだありません）")


def build_prompt(cands):
    examples, rejected = load_examples()
    style = STYLE.read_text(encoding="utf-8") if STYLE.exists() else "（まだありません）"
    selection = SELECTION.read_text(encoding="utf-8") if SELECTION.exists() else "（まだありません）"
    listing = "\n\n".join(
        f"[{i}] 出典: {c['source']}\n見出し: {c['title']}\n概要: {c['summary'] or '（なし）'}" for i, c in enumerate(cands))
    return PROMPT.format(n=PICK_COUNT, max_papers=MAX_PAPERS, style=style, selection=selection, examples=examples,
                         rejected=rejected, items=listing)


def call_api(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY が設定されていません（GitHub の Settings → Secrets に登録してください）")
    body = json.dumps({"model": MODEL, "max_tokens": 4000,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.loads(r.read())
    return "".join(b.get("text", "") for b in res.get("content", []))


def ask_claude(cands):
    text = call_api(build_prompt(cands))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Claude の返答からJSONが読み取れませんでした:\n" + text[:500])
    return json.loads(m.group(0))["picks"]


def slot_label():
    h = NOW.hour
    return "12時" if h < 15 else "18時" if h < 20 else "22時"


def main():
    POSTS.mkdir(exist_ok=True)
    seen = known_urls()
    cands = [c for c in collect_rss() + collect_pubmed() if c["url"] and c["url"] not in seen]
    # 同じURLの重複を除く
    uniq, urls = [], set()
    for c in cands:
        if c["url"] not in urls:
            uniq.append(c)
            urls.add(c["url"])
    cands = uniq[:120]
    print("\n".join(feed_status))
    print(f"候補: {len(cands)}件")
    if not cands:
        return

    picks = ask_claude(cands)
    stamp = NOW.strftime("%Y-%m-%d %H:00")
    written = []
    for n, p in enumerate(picks[:PICK_COUNT], 1):
        try:
            if p["cat"] not in CATEGORIES:
                raise ValueError(f"カテゴリ {p['cat']} は使えません")
            c = cands[int(p["index"])]
            post = {"cat": p["cat"], "title": p["title"].strip(), "points": [s.strip() for s in p["points"]][:3],
                    "comment": p["comment"].strip(), "source": c["source"], "url": c["url"], "published": stamp}
            text = render_post(post)
            parse_post(text)  # 形式チェック
        except Exception as e:  # noqa: BLE001
            print(f"スキップ: {p}（{e}）")
            continue
        path = POSTS / f"{NOW.strftime('%Y-%m-%d-%H%M')}-{n}.md"
        path.write_text(text, encoding="utf-8")
        DRAFTS.mkdir(exist_ok=True)
        (DRAFTS / f"{path.stem}.txt").write_text(post["comment"], encoding="utf-8")
        written.append((path.name, post))

    # PR の説明文（承認する人向け）
    lines = [f"## {NOW.strftime('%m/%d')} {slot_label()}配信の候補 {len(written)}本", "",
             "**確認のしかた**", "1. 「Files changed」で各記事を確認",
             "2. 直したい記事は「…」→「Edit file」で、見出し・ポイント・医師のひとことを編集",
             "3. 載せない記事は `公開: する` を `公開: しない（理由）` に変更。例：`公開: しない（専門的すぎる）`",
             "   理由は一言でOK。次回からの記事選びに反映されます",
             "4. 問題なければ「Merge pull request」→ 数分でサイトに反映", ""]
    for name, p in written:
        lines += [f"### {CATEGORIES[p['cat']]}｜{p['title']}", f"出典: [{p['source']}]({p['url']})", "",
                  *[f"- {s}" for s in p["points"]], "", f"> {p['comment']}", "", f"`posts/{name}`", ""]
    lines += ["---", "**情報源ごとの取得結果**", *feed_status]
    (ROOT / "pr-body.md").write_text("\n".join(lines), encoding="utf-8")
    env = os.environ.get("GITHUB_ENV")
    if env:
        with open(env, "a", encoding="utf-8") as f:
            f.write(f"PR_TITLE={NOW.strftime('%m/%d')} {slot_label()}配信の候補（{len(written)}本）\n")
    print(f"{len(written)}本の記事ファイルを作成しました")


if __name__ == "__main__":
    main()
