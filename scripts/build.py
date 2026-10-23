"""posts/*.md（承認済みの記事）から articles.json を作る。
GitHub Actions（.github/workflows/publish.yml）が、main に変更が入るたびに実行する。
記事がまだ1本もないときは、今の articles.json（サンプル）をそのまま使う。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from postfmt import HIDDEN, parse_post  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEEP = 150  # サイトに載せる最大本数（古いものから外れる）


def main():
    files = sorted((ROOT / "posts").glob("*.md"), reverse=True)
    if not files:
        print("posts/ に記事がないので、サンプルの articles.json のまま公開します")
        return
    arts, errors = [], []
    for f in files:
        try:
            p = parse_post(f.read_text(encoding="utf-8"))
        except ValueError as e:
            errors.append(f"{f.name}: {e}")
            continue
        p.pop("reason", None)
        if p.pop("publish") and p["cat"] not in HIDDEN:
            p["id"] = f.stem
            arts.append(p)
    arts.sort(key=lambda a: (a["date"], a["time"], a["id"]), reverse=True)
    out = {"updated": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="minutes"),
           "articles": arts[:KEEP]}
    (ROOT / "articles.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(arts[:KEEP])}本を articles.json に書き出しました")
    if errors:
        print("::warning::読み込めなかった記事があります（サイトには載りません）\n" + "\n".join(errors))


if __name__ == "__main__":
    main()
