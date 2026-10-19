"""Show summary metadata (not patient text) or remove an exact owner/session summary."""
import argparse
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from app.context_compaction import SummaryStore, estimate

ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True, help="Backend user ID, not email")
    parser.add_argument("--session", required=True)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    store = SummaryStore(os.getenv("CONTEXT_SUMMARY_DB", str(ROOT / ".cache" / "context_summaries.sqlite3")))
    key = (args.owner, args.session)
    if args.delete:
        store.delete(key)
        print("该会话的滚动摘要已清除；原始聊天记录未修改。")
        return
    row = store.load(key)
    print(json.dumps({"exists":bool(row), "version": row["version"] if row else None,
                      "cursor":row["cursor"] if row else 0,
                      "summary_estimate":estimate(row["summary"]) if row else 0}, indent=2))


if __name__ == "__main__":
    main()
