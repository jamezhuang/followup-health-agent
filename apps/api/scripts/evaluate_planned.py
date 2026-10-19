import argparse
import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scripts.evaluate_rag import (
    CatalogLookup, EvalAnswer, PROMPT, Suite, answer_metrics,
    answer_payload, digest, retrieval_metrics, save_report,
)

BASE = Path(__file__).resolve().parents[1]
MODES = ("dense", "keyword", "hybrid", "planned")


def summary(cases):
    def mean(items, keys):
        result = {"count": len(items)}
        for key in keys:
            values = [item[key] for item in items if item.get(key) is not None]
            result[key] = {"mean": sum(values) / len(values) if values else None, "n": len(values)}
        return result
    metrics = ["hit", "group_recall", "rr", "hospital_first_match", "hospital_precision"]
    result = {mode: mean([case["retrieval"][mode]["metrics"] for case in cases], metrics) for mode in MODES}
    result["answers"] = {
        mode: mean([case["answers"][mode]["metrics"] for case in cases if mode in case.get("answers", {})],
                   ["citation_id_rate", "quote_match_rate", "citation_group_coverage", "abstention_match", "evidence_structure_pass"])
        for mode in ("hybrid", "planned")
    }
    result["baseline_failures"] = sum("baseline_error_code" in case for case in cases)
    result["planned_failures"] = sum("planned_error_code" in case for case in cases)
    result["planner_fallbacks"] = sum(case.get("strategy") == "baseline_fallback" for case in cases)
    result["rerank_fallbacks"] = sum(branch.get("rerank", {}).get("status") == "fallback"
                                     for case in cases for branch in case.get("branches", []))
    result["rerank_logical_calls"] = sum(branch.get("rerank", {}).get("model_calls", 0)
                                         for case in cases for branch in case.get("branches", []))
    result["answer_failures"] = sum("error_code" in answer for case in cases for answer in case.get("answers", {}).values())
    return result


async def run(args):
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env", override=False)
    from app.config import Settings
    from app.hybrid import HybridKnowledgeSearch
    from app.planned_search import PlannedKnowledgeSearch, PLANNING_PROMPT
    from app.evidence_rerank import RERANK_PROMPT
    from app.providers import APIClient, ProviderError

    raw_suite = args.suite.read_bytes()
    suite = Suite.model_validate_json(raw_suite)
    if args.limit is not None and not 1 <= args.limit <= 1000:
        raise ValueError("limit 必须为 1 到 1000 的整数")
    wanted = set(args.case_ids or [])
    unknown = wanted - {case.id for case in suite.cases}
    if unknown:
        raise ValueError("未知题目 ID：" + ", ".join(sorted(unknown)))
    selected = [case for case in suite.cases if not wanted or case.id in wanted]
    if args.limit:
        selected = selected[:args.limit]
    settings = Settings()
    if settings.mode != "live":
        raise ValueError("测评需要 PROVIDER_MODE=live")
    client = APIClient(settings)
    baseline = HybridKnowledgeSearch(settings, client, mode="hybrid")
    planned = None
    try:
        planned = PlannedKnowledgeSearch(settings, client, mode="hybrid")
        if not planned.planning_enabled:
            raise ValueError("对比测评需要 RAG_QUERY_PLANNING_ENABLED=true")
        lookup = CatalogLookup(baseline.keyword.engine)
        before = await asyncio.to_thread(lookup.snapshot)
        available = {row["document_id"] for row in before if row["metadata_revision"]}
        required = {doc for case in selected for group in case.gold_groups for doc in group}
        if required - available:
            raise ValueError("标准资料未入库或未同步：" + ", ".join(sorted(required - available)))
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = args.out or BASE / "data/eval_runs" / f"gd_planned_{now}_{uuid.uuid4().hex[:8]}.json"
        report = {
            "suite_version": suite.version, "split": suite.split,
            "suite_sha256": hashlib.sha256(raw_suite).hexdigest(),
            "started_at": now, "complete": False, "corpus_unchanged": None,
            "corpus_sha256": digest(before), "corpus_snapshot": before,
            "selected_ids": [case.id for case in selected], "top_k": args.top_k,
            "implementation_sha256": {
                name: hashlib.sha256((BASE / name).read_bytes()).hexdigest()
                for name in ("app/rag.py", "app/hybrid.py", "app/planned_search.py", "app/evidence_rerank.py",
                             "scripts/evaluate_rag.py", "scripts/evaluate_planned.py")
            },
            "settings": {"embedding_model": settings.embedding_model, "dimensions": settings.dimensions,
                         "chat_model": settings.chat_model, "candidates": baseline.candidates,
                         "rerank_enabled": getattr(planned, "rerank_enabled", False),
                         "rerank_prompt_sha256": digest(RERANK_PROMPT),
                         "answer_prompt_sha256": digest(PROMPT), "planning_prompt_sha256": digest(PLANNING_PROMPT)},
            "scope": "原问题的 dense/keyword/hybrid 与规划检索的配对回归；回答采用相同冻结资料提示词，不经过线上 Agent。",
            "cases": [],
        }
        save_report(output, report)

        for case in selected:
            result = {"case": case.model_dump(), "retrieval": {}, "answers": {}}
            started = time.perf_counter()
            try:
                detail = await baseline.search_detailed(case.query, args.top_k)
                result["baseline_warnings"] = detail["warnings"]
                for mode, key in (("dense", "dense"), ("keyword", "keyword"), ("hybrid", "results")):
                    rows = await asyncio.to_thread(lookup.resolve, detail[key][:args.top_k])
                    result["retrieval"][mode] = {"rows": rows, "metrics": retrieval_metrics(case, rows)}
            except ProviderError as error:
                result["baseline_error_code"] = error.code
                for mode in MODES[:3]:
                    result["retrieval"][mode] = {"rows": [], "metrics": retrieval_metrics(case, [])}
            result["baseline_total_ms"] = round((time.perf_counter() - started) * 1000, 2)

            started = time.perf_counter()
            try:
                detail = await planned.search_detailed(case.query, args.top_k)
                result.update({"strategy": detail["strategy"], "query_plan": detail["plan"],
                               "planned_warnings": detail["warnings"], "branches": detail["branches"]})
                rows = await asyncio.to_thread(lookup.resolve, detail["results"])
                result["retrieval"]["planned"] = {"rows": rows, "metrics": retrieval_metrics(case, rows)}
            except ProviderError as error:
                result["planned_error_code"] = error.code
                result["retrieval"]["planned"] = {"rows": [], "metrics": retrieval_metrics(case, [])}
            result["planned_total_ms"] = round((time.perf_counter() - started) * 1000, 2)

            if args.with_answers:
                for mode, error_key in (("hybrid", "baseline_error_code"), ("planned", "planned_error_code")):
                    rows = result["retrieval"][mode]["rows"]
                    entry = {}
                    if error_key in result:
                        entry["error_code"] = "retrieval_failed"
                    else:
                        try:
                            answer = await client.structured(EvalAnswer, PROMPT, answer_payload(case, rows))
                            entry = {"answer": answer.model_dump(), "metrics": answer_metrics(case, answer, rows)}
                        except ProviderError as error:
                            entry["error_code"] = error.code
                    if "error_code" in entry:
                        entry["metrics"] = {"citation_id_rate": None, "quote_match_rate": None,
                                            "citation_group_coverage": 0.0 if case.gold_groups else None,
                                            "abstention_match": False, "evidence_structure_pass": False,
                                            "semantic_support_review": "generation_failed"}
                    result["answers"][mode] = entry

            report["cases"].append(result)
            report["summary"] = summary(report["cases"])
            save_report(output, report)
            old = result["retrieval"]["hybrid"]["metrics"]["group_recall"]
            new = result["retrieval"]["planned"]["metrics"]["group_recall"]
            print(f"{len(report['cases'])}/{len(selected)} {case.id} evidence_groups: hybrid={old} planned={new}")

        report["corpus_unchanged"] = digest(await asyncio.to_thread(lookup.snapshot)) == report["corpus_sha256"]
        report["complete"] = True
        save_report(output, report)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"对比报告：{output}")
        return 0 if report["corpus_unchanged"] else 2
    finally:
        if planned is not None:
            planned.close()
        baseline.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="比较原检索与分支规划检索")
    parser.add_argument("--suite", type=Path, default=BASE / "data/eval/gd_eval_v1.json")
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--limit", type=int, metavar="N", help="最多执行 N 道题，范围 1..1000")
    parser.add_argument("--top-k", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--with-answers", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    from app.providers import ProviderError
    from sqlalchemy.exc import SQLAlchemyError
    try:
        raise SystemExit(asyncio.run(run(args)))
    except ProviderError as error:
        raise SystemExit(f"{error.code}: {error.public_message}") from None
    except SQLAlchemyError as error:
        cause = getattr(error, "orig", None)
        diagnostic = getattr(cause, "diag", None)
        print("数据库异常：", type(error).__name__, getattr(cause, "sqlstate", None))
        print("具体原因：", getattr(diagnostic, "message_primary", None) or str(cause or error))
        raise SystemExit(1) from None
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
