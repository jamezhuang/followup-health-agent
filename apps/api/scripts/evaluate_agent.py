"""离线故障测试、真实会话测评、人工复核与回归对比。"""
import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET

import httpx
from app.agent_evaluation import apply_reviews, evaluate_round, load_cases, write_report, write_review_template

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_records(path, records):
    Path(path).write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in records), encoding="utf-8")


async def trace(client, run_id):
    if not run_id:
        return {"available": False, "reason": "no_run_id", "events": []}
    try:
        response = await client.get("/workspace/runs/"+quote(run_id, safe=""), timeout=15)
        data = response.json()
        if response.status_code != 200 or not isinstance(data, dict):
            return {"available": False, "status": response.status_code, "events": []}
        return {"available": True, "events": data.get("events") or [],
                "server_analysis": data.get("analysis") or [], "server_status": data.get("status")}
    except (httpx.HTTPError, ValueError):
        return {"available": False, "reason": "trace_fetch_failed", "events": []}


async def send_round(client, session_id, message, previous_run_id=""):
    started = time.monotonic()
    row = {"session_id": session_id, "input": message, "started_at": now(), "status": None,
           "result": None, "facts": {}, "run_id": "", "error_code": "", "reconciled": False}
    try:
        response = await client.post("/sessions/"+quote(session_id, safe="")+"/messages", json={"message": message})
        row["status"] = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = {}
            row["error_code"] = "invalid_response_json"
        row["response_json"] = data
        if isinstance(data, dict):
            row["facts"] = data.get("facts") or {}
            row["result"] = data.get("last_turn") or data.get("last_result")
            row["error_code"] = row["error_code"] or data.get("code", "")
            row["run_id"] = data.get("run_id", "")
        if isinstance(row["result"], dict):
            row["run_id"] = row["result"].get("run_id", "")
            if previous_run_id and row["run_id"] == previous_run_id:
                row.update(result=None, error_code="missing_result")
    except httpx.TimeoutException:
        row["error_code"] = "client_timeout"
    except httpx.HTTPError:
        row["error_code"] = "client_network_error"
    # 超时不重复发送POST；仅尝试读取已保存状态一次。
    if row["error_code"] in {"client_timeout", "client_network_error"}:
        original_error = row["error_code"]
        try:
            response = await client.get("/sessions/"+quote(session_id, safe=""), timeout=15)
            data = response.json()
            result = (data.get("last_turn") or data.get("last_result")) if isinstance(data, dict) else None
            users = [m for m in data.get("messages", []) if m.get("role") == "user"] if isinstance(data, dict) else []
            if (response.status_code == 200 and isinstance(result, dict) and result.get("run_id")
                    and result["run_id"] != previous_run_id and users and users[-1].get("content") == message):
                row.update(status=200, result=result, facts=data.get("facts") or {}, run_id=result["run_id"],
                           error_code="", reconciled=True, response_json=data, original_transport_error=original_error)
        except (httpx.HTTPError, ValueError):
            pass
    row["elapsed_ms"] = round((time.monotonic()-started)*1000, 2)
    row["trace"] = await trace(client, row["run_id"])
    return row


async def create_session(client):
    response = await client.post("/sessions")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise ValueError("新会话响应缺少id")
    return data["id"]


async def run_cases(client, cases, repeats, output):
    records = []
    total = len(cases)*repeats
    for case in cases:
        for repeat in range(1, repeats+1):
            print(f"{len(records)+1}/{total} {case['id']} 重复{repeat}", flush=True)
            record = {"case_id": case["id"], "dimension": case["dimension"], "title": case["title"],
                      "rubric": case["rubric"], "repeat": repeat, "mode": "live_api_evaluation", "rounds": [],
                      "review": {}, "started_at": now()}
            session_id, previous_run_id = "", ""
            try:
                session_id = await create_session(client)
                for index, turn in enumerate(case["turns"]):
                    if turn.get("new_session"):
                        session_id = await create_session(client)
                        previous_run_id = ""
                    print(f"  消息{index+1}/{len(case['turns'])}", flush=True)
                    row = await send_round(client, session_id, turn["message"], previous_run_id)
                    row["checks"] = evaluate_round(row, turn.get("checks"))
                    record["rounds"].append(row)
                    previous_run_id = row["run_id"] or previous_run_id
                    if row["status"] != 200:
                        break
            except (httpx.HTTPError, ValueError) as error:
                record["runner_error"] = type(error).__name__
                record["rounds"].append({"session_id": session_id, "status": None, "result": None,
                                         "error_code": "session_setup_failed", "trace": {"events": []},
                                         "checks": {"auto_pass": False, "errors": [{"code": "session_setup_failed"}]}})
            record["completed_all_turns"] = len(record["rounds"]) == len(case["turns"])
            record["auto_pass"] = record["completed_all_turns"] and all(r["checks"]["auto_pass"] for r in record["rounds"])
            record["completed_at"] = now()
            records.append(record)
            # 每个完成案例立即写入，后续中断仍能复盘已运行部分。
            write_records(output/"results.jsonl", records)
            write_report(output, records)
            write_review_template(output/"review.csv", records)
    return records


def compare_baseline(directory, baseline):
    previous = Path(baseline)
    current_manifest = json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
    previous_manifest = json.loads((previous/"manifest.json").read_text(encoding="utf-8"))
    current = json.loads((directory/"report.json").read_text(encoding="utf-8"))
    old = json.loads((previous/"report.json").read_text(encoding="utf-8"))
    comparable = current_manifest.get("selected_cases_sha256") == previous_manifest.get("selected_cases_sha256")
    delta = {"same_case_definitions": comparable, "current_cases": current["case_runs"], "baseline_cases": old["case_runs"],
             "warning": "样本量、运行配置、网页更新及人工复核状态仍需核对；差值不证明因果。", "metrics": {}}
    for key in ("response_rate", "automatic_contract_pass_rate", "citation_match_rate"):
        a, b = current.get(key), old.get(key)
        delta["metrics"][key] = {"current": a, "baseline": b, "delta": a-b if comparable and a is not None and b is not None else None}
    save_json(directory/"comparison.json", delta)


def run_faults(output):
    ledger = output/"fault_events.jsonl"
    env = {**os.environ, "AGENT_EVAL_FAULT_LEDGER": str(ledger)}
    command = [sys.executable, "-m", "pytest", str(ROOT/"tests/test_agent_faults.py"), "-q", "--tb=short",
               "--junitxml="+str(output/"faults.junit.xml")]
    print("运行离线故障与测评器测试；不调用真实模型、搜索服务或数据库。", flush=True)
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        save_json(output/"fault_report.json", {"mode": "offline_fault_injection", "error": "test_process_timeout", "live_quality_evaluated": False})
        return 1
    (output/"faults.log").write_text(result.stdout+"\n"+result.stderr, encoding="utf-8")
    tests = []
    if (output/"faults.junit.xml").exists():
        for case in ET.parse(output/"faults.junit.xml").getroot().iter("testcase"):
            state = "failed" if case.find("failure") is not None else "error" if case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"
            tests.append({"name": case.get("name"), "status": state, "seconds": case.get("time")})
    report = {"mode": "offline_fault_injection", "exit_code": result.returncode, "tests": tests,
              "passed": sum(t["status"] == "passed" for t in tests), "total": len(tests),
              "live_quality_evaluated": False, "semantic_correctness_evaluated": False}
    save_json(output/"fault_report.json", report)
    (output/"fault_report.md").write_text("# 离线故障测试\n\n"+f"通过{report['passed']}/{report['total']}项；退出码{result.returncode}。\n\n"
        +"只验证模拟故障下的流程与测评器，不证明真实医学回答没有幻觉。\n\n"
        +"\n".join(f"- {t['name']}：{t['status']}" for t in tests), encoding="utf-8")
    print(result.stdout, flush=True)
    return 0 if result.returncode == 0 and tests else 1


def manifest(args, cases):
    files = ["app/agent.py", "app/tool_loop.py", "app/answer_policy.py", "app/tool_policy.py", "app/schemas.py", "app/planned_search.py"]
    return {"started_at": now(), "suite": args.suite, "base_url": args.base_url,
            "repeat": args.repeat, "timeout_seconds": args.timeout,
            "case_ids": [c["id"] for c in cases],
            "selected_cases_sha256": hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "code_sha256": {f: digest(ROOT/f) for f in files if (ROOT/f).is_file()},
            "python": sys.version.split()[0], "credentials_recorded": False}


async def live(args, cases, output):
    email = args.email or os.getenv("AGENT_EVAL_EMAIL") or input("测试账号邮箱：").strip()
    password = os.getenv("AGENT_EVAL_PASSWORD") or getpass.getpass("测试账号密码（不回显、不写报告）：")
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout, trust_env=False, follow_redirects=False) as client:
        response = await client.post("/auth/login", json={"email": email, "password": password})
        if response.status_code != 200:
            raise RuntimeError(f"测试账号登录失败，HTTP {response.status_code}；未开始案例。")
        await run_cases(client, cases, args.repeat, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["smoke", "full", "faults"], default="smoke")
    parser.add_argument("--cases", type=Path, default=ROOT/"evaluation/agent_cases.jsonl")
    parser.add_argument("--ids", help="逗号分隔的案例ID")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--email")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--from-dir", type=Path, help="重建已有报告，不调用API")
    parser.add_argument("--review", type=Path, help="应用人工复核CSV")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 5 or args.timeout <= 0 or (args.limit is not None and args.limit < 1):
        parser.error("repeat为1..5，timeout和limit必须为正值")
    if args.from_dir:
        records = [json.loads(line) for line in (args.from_dir/"results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        if args.review:
            apply_reviews(records, args.review)
        write_report(args.from_dir, records)
        write_records(args.from_dir/"results.jsonl", records)
        if args.baseline:
            compare_baseline(args.from_dir, args.baseline)
        print("已重建报告：", args.from_dir)
        return 0
    if args.review:
        parser.error("--review需要同时提供--from-dir")
    cases = load_cases(args.cases) if args.suite != "faults" else []
    if args.list:
        for case in cases:
            print(f"{case['id']} [{case['dimension']}] {case['title']}：{len(case['turns'])}轮")
        return 0
    if args.ids:
        selected = set(args.ids.split(","))
        unknown = selected-{c["id"] for c in cases}
        if unknown:
            parser.error("未知案例ID："+",".join(sorted(unknown)))
        cases = [c for c in cases if c["id"] in selected]
    elif args.suite == "smoke":
        cases = [c for c in cases if "smoke" in c.get("tags", [])]
    if args.limit:
        cases = cases[:args.limit]
    if args.suite != "faults" and not cases:
        parser.error("没有选中的案例")
    output = (args.output or ROOT/".cache/agent_evaluation"/datetime.now().strftime("%Y%m%d_%H%M%S_%f")).resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("输出目录非空；请选择新目录以保留已有实验")
    output.mkdir(parents=True, exist_ok=True)
    save_json(output/"manifest.json", manifest(args, cases))
    save_json(output/"cases_snapshot.json", cases)
    if args.suite == "faults":
        code = run_faults(output)
    else:
        print(f"选中{len(cases)}个案例，重复{args.repeat}次，最多发送{sum(len(c['turns']) for c in cases)*args.repeat}条消息。", flush=True)
        asyncio.run(live(args, cases, output))
        if args.baseline:
            compare_baseline(output, args.baseline)
        code = 0
    print("结果目录：", output, flush=True)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, httpx.HTTPError) as error:
        print("测评未完成：", str(error), file=sys.stderr)
        raise SystemExit(1)
