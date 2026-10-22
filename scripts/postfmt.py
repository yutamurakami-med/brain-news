"""記事ファイル（posts/*.md）の読み書き。collect.py と build.py の両方から使う。

記事ファイルの形（Fumiさんが GitHub 上で直接編集する想定）:

---
公開: する
カテゴリ: 脳神経
タイトル: 〇〇〇
出典: PR TIMES（株式会社〇〇）
URL: https://...
配信: 2026-10-22 12:00
---
## ポイント
- 1つ目
- 2つ目
- 3つ目

## 医師のひとこと
コメント本文
"""
import re

CATEGORIES = {
    "brain": "脳神経",
    "longev": "抗加齢・ロンジェビティ",
    "prev": "予防医療",
    "career": "医師キャリア・副業",
    "policy": "政策・規制",
}
NAME_TO_ID = {v: k for k, v in CATEGORIES.items()}


def render_post(p):
    lines = [
        "---",
        "公開: する",
        f"カテゴリ: {CATEGORIES.get(p['cat'], p['cat'])}",
        f"タイトル: {p['title']}",
        f"出典: {p['source']}",
        f"URL: {p['url']}",
        f"配信: {p['published']}",
        "---",
        "## ポイント",
        *[f"- {s}" for s in p["points"]],
        "",
        "## 医師のひとこと",
        p["comment"],
        "",
    ]
    return "\n".join(lines)


def parse_post(text):
    """壊れていたら ValueError を出す。"""
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n(.*)", text, re.S)
    if not m:
        raise ValueError("先頭の --- で囲まれた部分が見つかりません")
    head, body = m.groups()
    meta = {}
    for line in head.splitlines():
        if not line.strip():
            continue
        k, sep, v = re.split(r"([:：])", line, maxsplit=1) if re.search(r"[:：]", line) else (line, "", "")
        if not sep:
            continue
        meta[k.strip()] = v.strip()
    for key in ("カテゴリ", "タイトル", "配信"):
        if not meta.get(key):
            raise ValueError(f"「{key}」が空です")
    cat = NAME_TO_ID.get(meta["カテゴリ"], meta["カテゴリ"])
    if cat not in CATEGORIES:
        raise ValueError(f"カテゴリ「{meta['カテゴリ']}」は使えません（使えるもの: {'、'.join(CATEGORIES.values())}）")

    sections = {}
    current = None
    for line in body.splitlines():
        h = re.match(r"##\s*(.+)", line)
        if h:
            current = h.group(1).strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    points = [re.sub(r"^\s*[-・*]\s*", "", l).strip() for l in sections.get("ポイント", []) if l.strip()]
    comment = "\n".join(sections.get("医師のひとこと", [])).strip()

    dm = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", meta["配信"])
    if not dm:
        raise ValueError("「配信」は 2026-10-22 12:00 の形で書いてください")
    return {
        "publish": meta.get("公開", "する") not in ("しない", "no", "いいえ", "×"),
        "cat": cat,
        "t": meta["タイトル"],
        "source": meta.get("出典", ""),
        "url": meta.get("URL", ""),
        "date": dm.group(1),
        "time": dm.group(2).zfill(5),
        "s": points,
        "m": comment,
    }
