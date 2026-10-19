"""回放 gd_planned 报告里的冻结候选，只比较增加语义重排这一项变化。"""

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.evidence_rerank import RERANK_PROMPT, rerank_evidence
from app.hybrid import fuse_rrf
from app.planned_search import SearchTask, matches_scope, select_balanced
from app.providers import ProviderError
from app.schemas import Source
from scripts.evaluate_rag import (
    Case, EvalAnswer, PROMPT, answer_metrics, answer_payload, digest,
    retrieval_metrics, save_report,
)


BASE = Path(__file__).resolve().parents[1]
MODES = ("planned_rrf", "planned_rerank")


def prepare_case(entry, top_k):
    """验证回放输入；先按线上相同边界过滤，再重建原 RRF 排名。"""
    case = Case.model_validate(entry["case"])
    if entry.get("strategy") != "planned" or "planned_error_code" in entry:
        raise ValueError(f"{case.id} 未成功执行规划检索，不能做候选回放")
    branches = []
    all_rows = {}
    raw_branches = entry.get("branches", [])
    if not 1 <= len(raw_branches) <= 3:
        raise ValueError(f"{case.id} 缺少有效分支候选记录")
    for branch in raw_branches:
        audit = branch.get("rerank", {})
        if audit.get("enabled") and audit.get("status") not in {"disabled", "skipped"}:
            raise ValueError("输入报告已应用重排，请使用此前未开启重排的 gd_planned 报告")
        task = SearchTask.model_validate(branch["task"])
        lists = []
        for mode in ("dense", "keyword"):
            kept = []
            for candidate in branch.get("candidates", {}).get(mode, []):
                source = Source.model_validate(candidate["source"])
                metadata = candidate.get("metadata") or {}
                allowed = matches_scope(task, metadata)
                if type(candidate.get("kept")) is not bool or candidate["kept"] != allowed:
                    raise ValueError(f"{case.id} 的候选过滤记录与当前边界规则不一致")
                if not allowed:
                    continue
                if not metadata.get("document_id"):
                    raise ValueError(f"{case.id} 缺少原文 ID，无法计算检索指标")
                row = {**metadata, "source": source.model_dump()}
                previous = all_rows.get(source.id)
                if previous:
                    def without_score(item):
                        return {**item, "source": {k: v for k, v in item["source"].items() if k != "score"}}
                    if without_score(previous) != without_score(row):
                        raise ValueError(f"{case.id} 同一候选 ID 对应不同正文或元数据")
                all_rows[source.id] = row
                kept.append(source)
            lists.append(kept)
        count = len({source.id for sources in lists for source in sources})
        if count > 20:
            raise ValueError("回放候选超过二十条，与当前检索窗口不一致")
        fused = fuse_rrf(lists, max(1, count))
        branches.append({"task": task, "sources": fused})
    baseline = select_balanced([branch["sources"][:top_k] for branch in branches], top_k)
    stored_ids = [row["source"]["id"] for row in entry["retrieval"]["planned"]["rows"]]
    if [source.id for source in baseline] != stored_ids:
        raise ValueError(f"{case.id} 无法重现原报告最终排序，停止比较以免混入其他变化")
    return case, branches, all_rows, baseline


def resolve_frozen(sources, rows):
    return [{**rows[source.id], "source": source.model_dump()} for source in sources]


def extended_metrics(case, rows, document_metadata):
    metrics = retrieval_metrics(case, rows)
    hospital_groups = [set(group) for group in case.gold_groups if any(
        document_metadata.get(doc, {}).get("category") in {"hospital_department", "hospital_service"}
        for doc in group
    )]
    returned = {row["document_id"] for row in rows}
    metrics["hospital_group_recall"] = (
        sum(bool(group & returned) for group in hospital_groups) / len(hospital_groups)
        if hospital_groups else None
    )
    return metrics


async def replay_case(client, entry, top_k, document_metadata, timeout_seconds=30):
    case, branches, rows, baseline = prepare_case(entry, top_k)
    started = time.perf_counter()
    # 一次只重排一个分支，避免测评批量并发冲击 API 限流。
    decisions = [await rerank_evidence(client, case.query, branch["task"],
                                       branch["sources"], top_k, timeout_seconds)
                 for branch in branches]
    selected = select_balanced([decision.sources for decision in decisions], top_k)
    result = {"case": case.model_dump(), "retrieval": {}, "answers": {},
              "rerank_ms": round((time.perf_counter() - started) * 1000, 2),
              "branches": [{"task": branch["task"].model_dump(), "rerank": decision.audit,
                            "warnings": decision.warnings,
                            "selected_ids": [source.id for source in decision.sources]}
                           for branch, decision in zip(branches, decisions)]}
    for mode, sources in zip(MODES, (baseline, selected)):
        resolved = resolve_frozen(sources, rows)
        result["retrieval"][mode] = {
            "rows": resolved, "metrics": extended_metrics(case, resolved, document_metadata),
        }
    return result


def summary(cases):
    def mean(items, keys):
        result = {"count": len(items)}
        for key in keys:
            values = [item[key] for item in items if item.get(key) is not None]
            result[key] = {"mean": sum(values) / len(values) if values else None, "n": len(values)}
        return result
    retrieval_keys = ["hit", "group_recall", "rr", "hospital_first_match",
                      "hospital_precision", "hospital_group_recall"]
    result = {mode: mean([case["retrieval"][mode]["metrics"] for case in cases], retrieval_keys)
              for mode in MODES}
    result["answers"] = {mode: mean(
        [case["answers"][mode]["metrics"] for case in cases if mode in case.get("answers", {})],
        ["citation_id_rate", "quote_match_rate", "citation_group_coverage",
         "abstention_match", "evidence_structure_pass"],
    ) for mode in MODES}
    result["rerank_fallbacks"] = sum(branch["rerank"]["status"] == "fallback"
                                     for case in cases for branch in case["branches"])
    result["rerank_logical_calls"] = sum(branch["rerank"]["model_calls"]
                                         for case in cases for branch in case["branches"])
    result["answer_failures"] = sum("error_code" in answer for case in cases
                                    for answer in case.get("answers", {}).values())
    return result


async def run(args):
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env", override=False)
    from app.config import Settings
    from app.providers import APIClient

    raw = args.input.read_bytes()
    original = json.loads(raw.decode("utf-8-sig"))
    if not original.get("complete") or original.get("corpus_unchanged") is not True:
        raise ValueError("必须使用完整且语料未变化的 gd_planned 报告")
    top_k = original.get("top_k")
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise ValueError("输入报告 top_k 不合法")
    requested = set(args.case_ids or [])
    available = {entry["case"]["id"] for entry in original["cases"]}
    if requested - available:
        raise ValueError("报告中不存在题目：" + ", ".join(sorted(requested - available)))
    entries = [entry for entry in original["cases"] if not requested or entry["case"]["id"] in requested]
    if not entries:
        raise ValueError("输入报告没有可测题目")
    if len({entry["case"]["id"] for entry in entries}) != len(entries):
        raise ValueError("输入报告题目 ID 重复")
    if not 0 < args.timeout_seconds <= 120:
        raise ValueError("timeout-seconds 必须大于 0 且不超过 120")
    document_metadata = {row["document_id"]: row for row in original["corpus_snapshot"]}
    for entry in entries:
        case, _, _, _ = prepare_case(entry, top_k)
        needed = {doc for group in case.gold_groups for doc in group}
        if needed - document_metadata.keys():
            raise ValueError(f"{case.id} 的标准资料不在冻结语料快照中")
    settings = Settings()
    if settings.mode != "live":
        raise ValueError("语义重排测评需要 PROVIDER_MODE=live")
    client = APIClient(settings)
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.out or BASE / "data/eval_runs" / f"gd_rerank_{now}_{uuid.uuid4().hex[:8]}.json"
    report = {
        "started_at": now, "complete": False, "input_sha256": hashlib.sha256(raw).hexdigest(),
        "suite_sha256": original["suite_sha256"], "corpus_sha256": original["corpus_sha256"],
        "top_k": top_k, "selected_ids": [entry["case"]["id"] for entry in entries],
        "settings": {"chat_model": settings.chat_model, "timeout_seconds": args.timeout_seconds,
                     "rerank_prompt_sha256": digest(RERANK_PROMPT), "answer_prompt_sha256": digest(PROMPT)},
        "implementation_sha256": {name: hashlib.sha256((BASE / name).read_bytes()).hexdigest()
                                  for name in ("app/evidence_rerank.py", "app/planned_search.py",
                                               "app/hybrid.py", "scripts/evaluate_rerank.py", "scripts/evaluate_rag.py")},
        "scope": "冻结上次报告的改写、候选、边界过滤和 top_k，只增加语义重排；不访问数据库、不重新向量化。",
        "cases": [],
    }
    save_report(output, report)
    for entry in entries:
        result = await replay_case(client, entry, top_k, document_metadata, args.timeout_seconds)
        if args.with_answers:
            case = Case.model_validate(entry["case"])
            for mode in MODES:
                rows = result["retrieval"][mode]["rows"]
                try:
                    answer = await client.structured(EvalAnswer, PROMPT, answer_payload(case, rows))
                    result["answers"][mode] = {"answer": answer.model_dump(),
                                              "metrics": answer_metrics(case, answer, rows)}
                except ProviderError as error:
                    result["answers"][mode] = {"error_code": error.code, "metrics": {
                        "citation_id_rate": None, "quote_match_rate": None,
                        "citation_group_coverage": 0.0 if case.gold_groups else None,
                        "abstention_match": False, "evidence_structure_pass": False,
                        "semantic_support_review": "generation_failed",
                    }}
        report["cases"].append(result)
        report["summary"] = summary(report["cases"])
        save_report(output, report)
        old, new = [result["retrieval"][mode]["metrics"]["group_recall"] for mode in MODES]
        print(f"{len(report['cases'])}/{len(entries)} {entry['case']['id']} group_recall: rrf={old} rerank={new}")
    report["complete"] = True
    save_report(output, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"重排回放报告：{output}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="对冻结候选比较规划 RRF 与规划 RRF+语义重排")
    parser.add_argument("--input", required=True, type=Path, help="此前未开启语义重排的 gd_planned JSON 报告")
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--with-answers", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except ProviderError as error:
        raise SystemExit(f"{error.code}: {error.public_message}") from None
    except (ValueError, OSError, KeyError) as error:
        raise SystemExit(str(error)) from None
