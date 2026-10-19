"""Private document acceptance: prepare / run / report. Python 3.11+."""
import argparse
import asyncio
import csv
import getpass
import hashlib
import json
import math
from pathlib import Path
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare(directory):
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("资料目录非空，请换新目录，避免覆盖实验。")
    directory.mkdir(parents=True, exist_ok=True)
    files = directory / "files"
    files.mkdir()
    manifest, cases = [], []
    for i in range(1, 21):
        suffix = [".txt", ".md", ".docx"][(i - 1) % 3]
        name = f"实验案例{i:02d}{suffix}"
        group = ["normal", "tail", "missing", "conflict"][(i - 1) % 4]
        age = 20 + i
        allergy = ["青霉素过敏", "磺胺类药物过敏", "未发现已知药物过敏", "头孢类药物过敏"][((i - 1) // 4) % 4]
        paragraphs = [f"# 软件测试虚构案例 {i:02d}", "本资料仅用于文件问答测评，不是真实患者资料，不应更新健康档案。"]
        if group == "tail":
            # 8 independent paragraphs put the fields beyond the first read page.
            paragraphs += [f"背景段落{j}：" + "测试资料归档说明，整理文件时需核对来源及记录范围。" * 24 for j in range(1, 9)]
        if group != "missing":
            paragraphs += [f"年龄：{age}岁。", f"过敏史：{allergy}。"]
        else:
            paragraphs += ["本文件未记录年龄，也未记录过敏史。"]
        paragraphs += [f"案例编号：CASE-{i:02d}。", "准备清单：携带既往检查报告、携带影像资料。"]
        path = files / name
        if suffix == ".docx":
            from docx import Document
            doc = Document()
            for paragraph in paragraphs:
                doc.add_paragraph(paragraph)
            doc.save(path)
        else:
            path.write_text("\n\n".join(paragraphs), encoding="utf-8")
        fid = f"D{i:02d}"
        manifest.append({"id": fid, "name": name, "group": group,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for field, value in [("age", str(age) + "岁"), ("allergy", allergy)]:
            missing = group == "missing"
            cases.append({"id": f"{fid}-{field}", "group": group, "document": fid,
                "question": f"请仅根据《{name}》回答其中记录的{'年龄' if field == 'age' else '过敏史'}。未记载就说未记载。这是虚构案例，请勿更新健康档案，也不需要推荐科室。",
                "expected": "未记载" if missing else value, "field": field,
                "answerable": not missing, "distractor": None})
    # Four conflict files receive another conflict document as a distractor.
    conflict = [d for d in manifest if d["group"] == "conflict"]
    for case in cases:
        if case["group"] == "conflict":
            idx = next(j for j, d in enumerate(conflict) if d["id"] == case["document"])
            case["distractor"] = conflict[(idx + 1) % len(conflict)]["id"]
    write_json(directory / "suite.json", {"version": 1, "documents": manifest, "cases": cases})
    print(f"已生成20份虚构文件、40道题：{directory}")


async def checked(client, method, url, **kwargs):
    response = await client.request(method, url, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"接口 {method} {url} HTTP {response.status_code}；不重复发送写请求。")
    return response.json()


async def wait_document(client, doc_id, wanted, seconds=180):
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        doc = await checked(client, "GET", f"/workspace/documents/{doc_id}")
        if doc["status"] in wanted:
            return doc
        if doc["status"] in {"failed", "deleted"}:
            raise RuntimeError(f"文件处理失败：{doc.get('error_code', '')}")
        await asyncio.sleep(1)
    raise RuntimeError("文件状态等待超时；未重发上传或确认请求。")


def answer_text(result):
    return "\n".join([result.get("summary", ""), *[c.get("text", "") for c in result.get("claims", [])]])


def evaluate_contract(result, target_name, run_id):
    sources = {s.get("id"): s for s in result.get("sources", [])}
    claims = result.get("claims", [])
    used = [sources.get(c.get("evidence_id"), {}) for c in claims]
    return {"citation_checks": [bool(c.get("quote")) and c.get("quote") in sources.get(c.get("evidence_id"), {}).get("text", "") for c in claims],
            "target_cited": any(s.get("kind") == "private_document" and s.get("title", "").startswith(target_name + " · ") for s in used),
            "off_target_private_citation": any(s.get("kind") == "private_document" and not s.get("title", "").startswith(target_name + " · ") for s in used),
            "run_id": run_id}


async def run(args):
    suite = json.loads((args.suite / "suite.json").read_text(encoding="utf-8"))
    selected = suite["cases"][:args.limit]
    docs = {d["id"]: d for d in suite["documents"]}
    needed = {c["document"] for c in selected} | {c["distractor"] for c in selected if c["distractor"]}
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("结果目录非空，请使用新目录；不自动续跑或重发问题。")
    args.output.mkdir(parents=True, exist_ok=True)
    email = input("独立测试账号邮箱（不要用真实患者账号）：").strip()
    password = getpass.getpass("测试账号密码（不回显、不写报告）：")
    rows, uploaded = [], {}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=240, trust_env=False) as client:
        await checked(client, "POST", "/auth/login", json={"email": email, "password": password})
        status = await checked(client, "GET", "/workspace/status")
        if not status.get("available"):
            raise RuntimeError("工作台不可用。")
        for fid in sorted(needed):
            item = docs[fid]
            path = args.suite / "files" / item["name"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError(f"测试文件被修改：{item['name']}")
            print("上传并确认虚构测试稿：", item["name"], flush=True)
            doc = await checked(client, "POST", "/workspace/documents", files={"file": (item["name"], path.read_bytes(), "application/octet-stream")})
            doc = await wait_document(client, doc["id"], {"awaiting_confirmation", "ready"})
            if doc["status"] != "ready":
                # Consent is limited to generated synthetic documents whose hash was checked.
                await checked(client, "PUT", f"/workspace/documents/{doc['id']}/confirm",
                              json={"revision": doc["revision"], "pieces": doc["pieces"]})
                doc = await wait_document(client, doc["id"], {"ready"})
            uploaded[fid] = {"id": doc["id"], "revision": doc["revision"], "name": doc["name"]}
            write_json(args.output / "uploaded.json", uploaded)
        write_json(args.output / "manifest.json", {"base_url": args.base_url, "variant": "current_agent", "backend_label": args.backend_label,
            "suite_sha256": hashlib.sha256((args.suite / "suite.json").read_bytes()).hexdigest(),
            "case_ids": [c["id"] for c in selected], "repeat": args.repeat,
            "credentials_recorded": False, "ablation_comparison": False, "provider_tokens": None})
        review = []
        for case in selected:
            for repeat in range(1, args.repeat + 1):
                print(f"{len(rows)+1}/{len(selected)*args.repeat} {case['id']}", flush=True)
                session = await checked(client, "POST", "/sessions")
                ids = [uploaded[case["document"]]["id"]]
                if case["distractor"]:
                    ids.append(uploaded[case["distractor"]]["id"])
                await checked(client, "PUT", f"/workspace/sessions/{session['id']}/selection", json={"document_ids": ids})
                row = {"case": case, "repeat": repeat, "session_id": session["id"], "status": None,
                       "result": {}, "events": [], "error_code": "", "trace_available": False}
                start = time.monotonic()
                try:
                    response = await client.post(f"/sessions/{session['id']}/messages", json={"message": case["question"]})
                    row["status"] = response.status_code
                    data = response.json()
                    row["result"] = data.get("last_turn") or data.get("last_result") or {}
                    row["error_code"] = data.get("code", "")
                    row["run_id"] = row["result"].get("run_id") or data.get("run_id", "")
                except (httpx.HTTPError, ValueError):
                    row["error_code"] = "transport_or_parse_error_no_post_retry"
                row["elapsed_ms"] = round((time.monotonic() - start) * 1000)
                row["answer"] = answer_text(row["result"])
                if row.get("run_id"):
                    try:
                        trace = await checked(client, "GET", "/workspace/runs/" + row["run_id"])
                        row["events"] = trace.get("events", [])
                        row["trace_available"] = True
                    except (httpx.HTTPError, RuntimeError, ValueError):
                        pass
                row["checks"] = evaluate_contract(row["result"], docs[case["document"]]["name"], row.get("run_id", ""))
                row["expected_literal_present"] = case["expected"] in row["answer"]  # Diagnostic only, never accuracy.
                rows.append(row)
                with (args.output / "results.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                review.append({"case_id": case["id"], "repeat": repeat, "field_correct": "", "unsupported_addition": "",
                               "false_complete_read": "", "notes": ""})
                with (args.output / "review.csv").open("w", encoding="utf-8-sig", newline="") as file:
                    writer = csv.DictWriter(file, fieldnames=list(review[0]))
                    writer.writeheader()
                    writer.writerows(review)
                report(args.output)
    print("完成。人工填写review.csv后再执行report：", args.output)


def ratio(values):
    return {"value": sum(values) / len(values) if values else None, "n": len(values)}


def report(directory):
    rows = [json.loads(line) for line in (directory / "results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    with (directory / "review.csv").open(encoding="utf-8-sig", newline="") as file:
        reviews = list(csv.DictReader(file))
    reviewed = {(r["case_id"], int(r["repeat"])): r for r in reviews}
    def labels(field, predicate=lambda r: True):
        result = []
        for row in rows:
            value = reviewed.get((row["case"]["id"], row["repeat"]), {}).get(field, "").strip()
            if value not in {"", "0", "1"}:
                raise ValueError("人工标签仅可填0/1或留空。")
            if predicate(row) and value:
                result.append(int(value))
        return result
    success = [r for r in rows if r["status"] == 200 and r["result"]]
    citations = [x for r in success for x in r["checks"]["citation_checks"]]
    elapsed = sorted(r["elapsed_ms"] for r in rows)
    traces = [r for r in rows if r["trace_available"]]
    summary = {"requests_recorded": len(rows), "http_success_rate": ratio([r in success for r in rows]),
        "target_citation_rate_successful_answerable": ratio([r["checks"]["target_cited"] for r in success if r["case"]["answerable"]]),
        "literal_quote_match_rate": ratio(citations),
        "off_target_private_citation_rate": ratio([r["checks"]["off_target_private_citation"] for r in success]),
        "field_accuracy_human_answerable": ratio(labels("field_correct", lambda r: r["case"]["answerable"])),
        "missing_field_correct_rate_human": ratio(labels("field_correct", lambda r: not r["case"]["answerable"])),
        "unsupported_addition_rate_missing_human": ratio(labels("unsupported_addition", lambda r: not r["case"]["answerable"])),
        "false_complete_read_rate_tail_human": ratio(labels("false_complete_read", lambda r: r["case"]["group"] == "tail")),
        "trace_coverage": ratio([r["trace_available"] for r in rows]),
        "tool_call_count_mean_traced": sum(sum(e.get("stage") == "agent_tool" and e.get("status") == "started" for e in r["events"]) for r in traces) / len(traces) if traces else None,
        "p50_ms": elapsed[math.ceil(len(elapsed)*.5)-1] if elapsed else None,
        "p95_ms": elapsed[math.ceil(len(elapsed)*.95)-1] if elapsed else None,
        "actual_provider_tokens": None, "baseline_improvement": None,
        "notes": "准确率依赖人工标签；空标签不计入分母，必须看n。字面出现不代表语义正确。当前仅验收，不证明较基线提升。"}
    write_json(directory / "report.json", summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "run", "report"])
    parser.add_argument("--suite", type=Path, default=ROOT / ".cache/private_document_eval/suite_v1")
    parser.add_argument("--output", type=Path, default=ROOT / ".cache/private_document_eval/run_01")
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--backend-label", default="ghcr-0.1.0")
    args = parser.parse_args()
    if not 1 <= args.limit <= 40 or not 1 <= args.repeat <= 3:
        parser.error("limit=1..40；repeat=1..3")
    if args.phase == "prepare":
        prepare(args.suite)
    elif args.phase == "run":
        asyncio.run(run(args))
    else:
        report(args.output)
        print((args.output / "report.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        print("实验未完成：", str(error))
        raise SystemExit(1)
