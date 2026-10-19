"""Live, resumable context acceptance. No extra judge-model calls."""
import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import quote, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "这是虚构测试账号的就诊准备计划，不是实际预约。只讨论准备事项，不替我预约，不需查询医院名单。"
BEFORE = [
    TOPIC + "我计划在广州周三上午到公立医院门诊，家人陪同，医院尚未选定。请记住这些计划条件，说明它们都不是已经确认的预约。",
    "我的计划重点是整理纸质报告原件和电子报告，医院尚未选定。请列出资料分类的方法，暂时不涉及任何诊断。回答约100到150字。",
    "继续这个准备计划。哪些事项适合前一天检查，哪些适合出门前检查？请给两组清单，不查询实时门诊信息。回答约100到150字。",
    "我会先写一张问题清单。如何把最关心的问题放在前面，并区分已知事实和待医生确认的问题？回答约100到150字。",
    "同行家人只是帮我记录信息。请解释怎样分工，避免家人的猜测变成我的病史记录。回答约100到150字。",
    "我想把电子报告按日期整理，但不把上传日期当检查日期。请说明这样的文件命名和整理方式。回答约100到150字。",
    "现在先讨论原件保护。请说说纸质资料怎样分装、电子副本怎样备份，避免把不同人的报告放在一起。回答约100到150字。",
    "在这个计划里，我还没有确定医院，也没有完成挂号。请解释准备计划和实际预约的区别，不要编造预约结果。回答约100到150字。",
    "更正前面的城市：计划改为佛山，广州只是旧计划。其余条件先保持不变，请以后把广州理解为已经取消的计划地点。",
    "针对刚才的城市更正，哪些准备清单可以继续用，哪些安排需要重新核对？只讲一般准备流程。回答约100到150字。",
    "我仍希望家人陪同。家人记录医生解释时，怎样区分原话、自己的理解和还需要追问的地方？回答约100到150字。",
    "我的报告既有纸质原件也有电子副本。请解释如何检查姓名、日期、单位这些信息的一致性，不解释具体检查结果。回答约100到150字。",
    "先换一个话题：用通俗语言解释RAG、向量检索和关键词检索各自做什么。这个技术问题不需要关联我的就诊准备计划。回答约100到150字。",
    "继续技术话题：为什么聊天历史中的旧引文不能自动当作当前问题的新证据？只解释来源与上下文的区别。回答约100到150字。",
    "回到虚构就诊准备计划，第二次更正：时间改为周五上午，周三上午取消。城市仍是更正后的地点，医院仍未选定，也没有实际挂号。",
    "资料内容发生修改后，旧候选信息为什么需要重新核对？请解释版本和用户确认的作用，不声称你已经更新我的档案。回答约100到150字。",
    "在准备清单里，我要区分已完成、待完成、需要现场核对三种状态。请给一般的整理方法，不把待办事项说成已经完成。回答约100到150字。",
    "如果某份报告缺少日期或单位，准备时怎样标记这个问题并向医生说明？只给资料整理建议。回答约100到150字。",
    "我没有要求你确定我的疾病，也没有提供足够的诊断依据。请解释自述、报告原文和医生诊断在信息整理中应怎样分别标记。回答约100到150字。",
    "请结合这轮对话，说明我现在应该如何保存准备清单与资料目录。重点说明已更正的条件优先于旧计划，回答约100到150字。",
    "现在复述我的当前计划：城市、星期与时段、医院是否已选定、是否实际挂号、陪同安排、资料形式。把旧城市和旧时间另列为已取消条件，不猜测我的疾病。",
]
AFTER = [
    "后端已经重启。请根据这一个会话的历史，复述当前计划的城市、星期与时段、医院是否已选定、是否实际挂号、陪同安排和资料形式，不把这次问题中未写出的信息当新输入。",
    "再换一个独立话题：简要解释JSON为什么适合结构化输出。不需要附带我的就诊计划或个人病史。",
    "回到前面的虚构准备计划。请最后整理一份简短清单，先写当前城市和时间，再写未完成事项，不能恢复已取消的城市或时间，也不能声称已替我预约。",
]
METRICS = {
    "status", "before_estimate_at_least", "after_estimate", "cursor_before", "cursor_after",
    "summary_calls", "summary_input_estimate", "summary_output_estimate", "pending_messages",
    "retained_messages", "omitted_recent_messages", "unsummarized_old_messages", "summary_estimate",
    "persistent", "reset", "persistence_error", "current_input_deduplicated", "estimator", "binding",
    "trigger_budget", "target_budget", "summary_budget", "recent_turns", "max_batches",
    "compaction_error",
}
TRACE_LABELS = {"stage", "status", "error_code", "tool", "stop_reason", "exception_type", "mode"}
TRACE_COUNTS = {"step", "repairs", "used_calls", "duration_ms", "source_count", "tool_call_count", "max_steps", "max_calls"}


def safe_trace_events(events):
    """Retain diagnostic codes, never free-form prompt, source or error bodies."""
    if not isinstance(events, list):
        return []
    result = []
    for event in events[-300:]:
        if not isinstance(event, dict):
            continue
        item = {}
        for key in TRACE_LABELS:
            value = event.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
                item[key] = value
        for key in TRACE_COUNTS:
            value = event.get(key)
            if type(value) is int and 0 <= value <= 10**12:
                item[key] = value
        if item.get("stage"):
            result.append(item)
    return result


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def history_estimate(messages):
    if not isinstance(messages, list):
        return None
    rows = []
    for row in messages:
        if not isinstance(row, dict) or row.get("role") not in {"user", "assistant"} or not isinstance(row.get("content"), str):
            return None
        rows.append({"role": row["role"], "content": row["content"]})
    return len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 16


def db_snapshot(path, owner, session):
    if not path.is_file():
        return {"available": False, "reason": "summary_db_not_found"}
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as db:
            row = db.execute("SELECT version,cursor,summary FROM context_summaries WHERE owner=? AND session=?",
                             (owner, session)).fetchone()
        if not row:
            return {"available": True, "exists": False}
        return {"available": True, "exists": True, "version": row[0], "cursor": row[1],
                "summary_sha256": hashlib.sha256(row[2].encode("utf-8")).hexdigest()}
    except sqlite3.Error:
        return {"available": False, "reason": "summary_db_read_failed"}


def analyze(rows, complete, restart):
    events = [row["context"] for row in rows if row.get("context")]
    enriched = [event for event in events if type(event.get("after_estimate")) is int
                and type(event.get("target_budget")) is int and type(event.get("trigger_budget")) is int]
    compressed = [event for event in enriched if event.get("status") == "compressed"]
    limits = [event["after_estimate"] <= (event["target_budget"] if event.get("summary_estimate", 0)
              or event.get("omitted_recent_messages", 0) else event["trigger_budget"]) for event in enriched]
    comparisons = [{"round": row["round"], "full_history_estimate": row["full_history_estimate"],
                    "model_history_estimate": row["context"]["after_estimate"],
                    "estimated_reduction_ratio": 1 - row["context"]["after_estimate"] / row["full_history_estimate"]}
                   for row in rows if row.get("context", {}).get("summary_estimate", 0) > 0
                   and type(row.get("full_history_estimate")) is int and row["full_history_estimate"] > 16
                   and type(row["context"].get("after_estimate")) is int]
    after_rows = [row for row in rows if row.get("phase") == "after"]
    pre = restart.get("saved_before_restart", {})
    restored = restart.get("read_before_first_after_message", {})
    first_context = after_rows[0].get("context", {}) if after_rows else {}
    if after_rows and pre.get("exists") and restored.get("exists"):
        recovery = (pre == restored and first_context.get("persistent") is True
                    and first_context.get("cursor_before") == pre["cursor"] and first_context.get("reset") is False)
    else:
        recovery = None
    return {
        "completed_24_rounds": complete,
        "rounds_recorded": len(rows), "successful_responses": sum(row.get("status") == 200 for row in rows),
        "context_events": len(events), "events_with_numeric_metrics": len(enriched),
        "summary_trigger_verified": bool(compressed),
        "history_budget_pass": all(limits) if enriched and len(enriched) == len(rows) else None,
        "summary_calls": sum(event.get("summary_calls", 0) for event in events),
        "summary_input_estimate_total": sum(event.get("summary_input_estimate", 0) for event in events),
        "summary_output_estimate_total": sum(event.get("summary_output_estimate", 0) for event in events),
        "fallback_rounds": [row["round"] for row in rows if row.get("context", {}).get("status") == "fallback"],
        "unbound_rounds": [row["round"] for row in rows if row.get("context", {}).get("binding") == "unbound_recent_only"],
        "restart_summary_recovery": recovery, "history_size_comparisons": comparisons,
        "actual_provider_tokens": None, "billing_savings_verified": False,
        "semantic_review": "pending_manual_review",
        "estimator": "utf8_byte_upper_bound",
        "failure_events": [
            {"round": row["round"], **event}
            for row in rows for event in safe_trace_events(row.get("events", []))
            if event.get("error_code") or event.get("status") == "failed"
        ],
    }


def report(directory, state):
    outcome = analyze(state["rows"], state.get("after_complete", False), state.get("restart", {}))
    save(directory / "checkpoint.json", state)
    save(directory / "report.json", outcome)
    text = ["# 长对话压缩验收", "", f"已记录 {outcome['rounds_recorded']}/24 轮；HTTP200 {outcome['successful_responses']} 轮。",
            f"确认触发总结：{outcome['summary_trigger_verified']}；历史预算检查：{outcome['history_budget_pass']}；摘要重启恢复：{outcome['restart_summary_recovery']}。", "",
            "null 表示缺少证据，不代表通过。统计仅覆盖模型历史窗口，不覆盖系统提示、当前输入、工具正文及全部模型调用。",
            "估算单位是 UTF-8 字节上界，不是模型实际计费 token。摘要调用有额外成本；本报告不证明账单下降。", "",
            "|轮次|HTTP|压缩状态|完整历史估算|模型历史估算|摘要调用|游标|", "|---|---|---|---|---|---|---|"]
    for row in state["rows"]:
        event = row.get("context", {})
        status = event.get('status', '无事件') + (' / ' + event['compaction_error'] if event.get('compaction_error') else '')
        text.append(f"|{row['round']}|{row.get('status')}|{status}|{row.get('full_history_estimate')}|{event.get('after_estimate')}|{event.get('summary_calls')}|{event.get('cursor_before')}→{event.get('cursor_after')}|")
    if outcome["failure_events"]:
        text.extend(["", "## 失败事件", "", "按执行顺序保留错误码；后续包装错误不能替代最早失败节点。", "",
                     "|轮次|阶段|状态|错误码|", "|---|---|---|---|"])
        for event in outcome["failure_events"]:
            text.append(f"|{event['round']}|{event.get('stage', '')}|{event.get('status', '')}|{event.get('error_code', '')}|")
    text.extend(["", "## 人工复核", "", "检查第21、22、24轮的完整回答（checkpoint.json）。正确条件：佛山、周五上午、医院未选定、未实际挂号、家人陪同、纸质原件与电子副本。广州与周三上午属于旧计划。",
                 "- [ ] 第21轮保留最新更正，未编造诊断或预约。",
                 "- [ ] 第22轮重启后仍能还原最新计划。",
                 "- [ ] 第13、14、23轮技术话题没有无理由混入就诊信息。",
                 "- [ ] 第24轮不恢复旧城市、旧时间。",
                 "", "## 本次状态", "", state.get("notice", "")])
    (directory / "report.md").write_text("\n".join(text), encoding="utf-8")


async def run(args):
    directory = args.output.resolve() if args.output else ROOT / ".cache/context_acceptance" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if args.phase == "after":
        if not args.output:
            raise ValueError("after阶段需要 --output 指向之前的结果目录。")
        state = json.loads((directory / "checkpoint.json").read_text(encoding="utf-8"))
        if not state.get("before_complete") or state.get("after_started"):
            raise ValueError("前21轮未完成，或after阶段已发送过；不能自动重发。")
    else:
        if directory.exists() and any(directory.iterdir()):
            raise ValueError("结果目录非空，请选新目录。")
        directory.mkdir(parents=True, exist_ok=True)
        state = {"rows": [], "session_id": "", "owner": "", "before_complete": False,
                 "after_complete": False, "base_url": args.base_url, "restart": {}, "notice": ""}
    email = args.email or input("测试账号邮箱：").strip()
    password = getpass.getpass("测试账号密码（不回显、不写报告）：")
    print("结果目录：", directory, flush=True)
    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout,
                                     trust_env=False, follow_redirects=False) as client:
            response = await client.post("/auth/login", json={"email": email, "password": password})
            if response.status_code != 200:
                raise ValueError(f"登录失败 HTTP{response.status_code}；未发送聊天。")
            who = await client.get("/auth/me")
            who.raise_for_status()
            user = who.json().get("user")
            if not isinstance(user, dict) or not user.get("id"):
                raise ValueError("无法取得登录账号ID；未开始聊天。")
            if args.phase == "after" and (state["owner"] != user["id"] or state["base_url"] != args.base_url):
                raise ValueError("账号或后端地址与前半段不同；未继续聊天。")
            state["owner"] = user["id"]
            if args.phase == "before":
                created = await client.post("/sessions")
                created.raise_for_status()
                state["session_id"] = created.json()["id"]
            path = "/sessions/" + quote(state["session_id"], safe="")
            current = await client.get(path)
            current.raise_for_status()
            messages = current.json().get("messages")
            if args.phase == "after":
                state["restart"]["read_before_first_after_message"] = db_snapshot(args.summary_db, state["owner"], state["session_id"])
            turns = BEFORE if args.phase == "before" else AFTER
            offset = 0 if args.phase == "before" else len(BEFORE)
            for index, message in enumerate(turns, 1):
                number = offset + index
                if args.phase == "after":
                    state["after_started"] = True
                # Save intent before POST. A timeout must not trigger duplicate sends.
                state["in_flight_round"] = number
                report(directory, state)
                print(f"{number}/24", flush=True)
                baseline = history_estimate(messages)
                if not isinstance(messages, list) or len(messages) < 2 * (number - 1):
                    baseline = None  # truncated/paginated API history is not a full-history baseline
                started = time.monotonic()
                row = {"round": number, "phase": args.phase, "input": message,
                       "status": None, "context": {}, "full_history_estimate": baseline}
                try:
                    response = await client.post(path + "/messages", json={"message": message})
                    row["status"] = response.status_code
                    data = response.json()
                    turn = data.get("last_turn") or data.get("last_result") or {}
                    row["run_id"] = turn.get("run_id") or data.get("run_id", "")
                    row["error_code"] = str(data.get("code", ""))[:80]
                    new_messages = data.get("messages")
                    answer = next((item["content"] for item in reversed(new_messages or [])
                                   if item.get("role") == "assistant"), "")
                    row["answer"] = answer.replace(password, "[REDACTED]") if password else answer
                    messages = new_messages
                    if row["run_id"]:
                        try:
                            trace = await client.get("/workspace/runs/" + quote(row["run_id"], safe=""), timeout=15)
                            if trace.status_code == 200:
                                events = trace.json().get("events") or []
                                row["events"] = safe_trace_events(events)
                                matched = [event for event in events if event.get("stage") == "context_compaction"]
                                if matched:
                                    row["context"] = {key: value for key, value in matched[-1].items() if key in METRICS}
                        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                            row["trace_available"] = False
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    row["transport_or_parse_error"] = True
                row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                state["rows"].append(row)
                state.pop("in_flight_round", None)
                report(directory, state)
                if row["status"] != 200 or row.get("transport_or_parse_error"):
                    raise ValueError(f"第{number}轮未完成；已保存记录，未重发POST。")
            if args.phase == "before":
                state["before_complete"] = True
                state["restart"]["saved_before_restart"] = db_snapshot(args.summary_db, state["owner"], state["session_id"])
                state["notice"] = "前21轮完成。停止并重启后端后，用after阶段继续同一会话；还需人工复核。"
            else:
                state["after_complete"] = True
                state["notice"] = "24轮完成；检查report.json统计并人工复核计划更正与话题切换。"
            report(directory, state)
            print(state["notice"], flush=True)
    except (httpx.HTTPError, ValueError, KeyError, OSError) as error:
        state["notice"] = str(error) if isinstance(error, ValueError) else "测试未完成：" + type(error).__name__
        report(directory, state)
        raise ValueError(state["notice"]) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["before", "after"], default="before")
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--email")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-db", type=Path, default=Path(os.getenv("CONTEXT_SUMMARY_DB", str(ROOT / ".cache/context_summaries.sqlite3"))))
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        parser.error("本验收脚本只连接本机HTTP后端。")
    if args.timeout <= 0:
        parser.error("timeout必须大于0。")
    if args.list:
        for number, question in enumerate(BEFORE + AFTER, 1):
            print(number, question)
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyboardInterrupt) as error:
        print("验收未完成：", str(error) if isinstance(error, ValueError) else type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
