import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from app.document_ingest import prepare

BASE = Path(__file__).resolve().parents[1]


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)

        os.replace(temporary, path)

    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="把公开资料转换成现有 RAG 的入库格式")

    parser.add_argument(
        "--manifest",
        type=Path,
        default=BASE / "data/knowledge_sources.json",
    )

    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=BASE / "data/knowledge_raw",
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=BASE / "data/knowledge_import.json",
    )

    args = parser.parse_args()

    sources, records = prepare(
        args.manifest,
        args.raw_dir,
    )

    content = json.dumps(
        sources,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    metadata = {
        "schema_version": 1,
        "import_sha256": hashlib.sha256(content).hexdigest(),
        "records": records,
    }

    atomic_write(args.out, content)

    atomic_write(
        args.out.with_suffix(".meta.json"),
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8"),
    )

    print(f"转换完成：sources={len(sources)} " f"output={args.out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(f"资料转换失败：{type(error).__name__}: {error}") from None
