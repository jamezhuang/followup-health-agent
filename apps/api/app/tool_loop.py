"""有预算的 Function Calling 执行循环，工具结果逐一关联 tool_call_id。"""
# CAREBRIEF_OUTPUT_REPAIR_HINT_V1
from .workspace.document_query_policy import output_repair_hint
# CAREBRIEF_RELIABILITY_V3
from .agent_reliability_v3 import evidence_tool_loop, final_repair_only, repair_instructions
# CAREBRIEF_RELIABILITY_V2
from .agent_reliability_v2 import efficient_tool_loop, retrieval_should_stop, final_repair_allowed
# CAREBRIEF_RELIABILITY_V1
from .agent_reliability import observed_tool_loop

import asyncio
import copy
import json
import logging
import time

from .providers import ProviderError

logger = logging.getLogger(__name__)


class ToolLoopError(RuntimeError):
    pass


@observed_tool_loop
@efficient_tool_loop
@evidence_tool_loop
async def run_tool_loop(
    client, messages, tools, handlers, *, max_steps=4, max_calls=6,
    tool_timeout=20, total_timeout=120, final_reserve=15,
    on_event=None, validate_final=None,
):
    if (max_steps < 1 or max_calls < 1 or tool_timeout <= 0
            or total_timeout <= final_reserve or final_reserve <= 0):
        raise ValueError("Agent limits must be positive and reserve less than total")
    messages = copy.deepcopy(messages)
    started = time.monotonic()
    used_calls = 0
    attempts = {}
    cached = {}
    repairs = 0
    seen_call_ids = set()

    async def emit(stage, **details):
        if on_event is not None:
            await on_event(stage, **details)

    def remaining():
        return total_timeout - (time.monotonic() - started)

    for step in range(max_steps + 2):
        stopping = step >= max_steps or used_calls >= max_calls or remaining() <= final_reserve or retrieval_should_stop() or final_repair_only()
        if stopping:
            messages.append({"role": "system", "content": (
                "工具预算已耗尽，停止调用工具。根据已经获得的结果输出最终JSON。"
                "没有充分资料时说明不足；不编造来源或事实。"
            )})
            await emit("agent_budget", status="stopped", step=step, used_calls=used_calls)
        if len(json.dumps(messages, ensure_ascii=False)) > 100000:
            raise ToolLoopError("Agent context exceeds limit")
        if remaining() <= 0:
            raise ToolLoopError("Agent time budget exceeded")
        await emit("agent_model", status="started", step=step, used_calls=used_calls)
        try:
            response = await asyncio.wait_for(
                client.complete(
                    messages, tools=tools, tool_choice="none" if stopping else "auto",
                    response_format={"type": "json_object"},
                    thinking={"type": "disabled"}, max_tokens=8000,
                ), timeout=remaining(),
            )
        except asyncio.TimeoutError:
            await emit("agent_model", status="failed", error_code="model_timeout", step=step)
            raise ToolLoopError("Model time budget exceeded") from None
        if not isinstance(response, dict):
            raise ToolLoopError("Invalid model response")
        calls = response.get("tool_calls") or []
        await emit("agent_model", status="ok", step=step,
                   tool_call_count=len(calls) if isinstance(calls, list) else 0)
        if not calls:
            content = response.get("content")
            feedback = "empty_answer"
            if isinstance(content, str) and content.strip():
                feedback = await validate_final(content) if validate_final else None
            if not feedback:
                await emit("agent_final", status="ok", used_calls=used_calls, repairs=repairs)
                return content
            await emit("agent_final", status="failed", error_code=feedback, repairs=repairs)
            if not final_repair_allowed(stopping, repairs, remaining()):
                raise ToolLoopError("Final answer failed validation")
            repairs += 1
            messages.extend([
                {"role": "assistant", "content": content or ""},
                {"role": "system", "content": (
                    ("上一份最终输出未通过校验，错误码：" + repair_instructions(feedback) +
                    "。请修正JSON结构和引用；只能引用本轮成功返回的正文，"
                    "quote必须是连续原文。可补充调用工具；资料不足时明确说明。" + output_repair_hint(feedback))
                )},
            ])
            continue
        if stopping or not isinstance(calls, list) or len(calls) > max_calls:
            raise ToolLoopError("Invalid tool batch")
        batch_ids = set()
        for call in calls:
            if (not isinstance(call, dict) or call.get("type") != "function"
                    or not isinstance(call.get("id"), str) or not call["id"]
                    or call["id"] in batch_ids or call["id"] in seen_call_ids
                    or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("name"), str)
                    or not isinstance(call["function"].get("arguments"), str)):
                raise ToolLoopError("Malformed tool call")
            batch_ids.add(call["id"])
        seen_call_ids.update(batch_ids)
        messages.append({"role": "assistant", "content": response.get("content"),
                         "tool_calls": calls})
        # 顺序执行允许同批的 read 依赖前面的 search；不盲目并行有依赖的调用。
        for call in calls:
            name = call["function"]["name"]
            await emit("agent_tool", status="started", tool=name, tool_call_id=call["id"], step=step)
            if used_calls >= max_calls or remaining() <= final_reserve:
                result = {"ok": False, "error": "tool_budget_exceeded", "retryable": False}
            else:
                used_calls += 1
                try:
                    args = json.loads(call["function"]["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError("not object")
                except ValueError:
                    result = {"ok": False, "error": "invalid_arguments", "retryable": False,
                              "hint": "按工具schema修正参数后再调用。"}
                else:
                    fingerprint = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                    if name not in handlers:
                        result = {"ok": False, "error": "unknown_tool", "retryable": False}
                    elif fingerprint in cached:
                        result = {**cached[fingerprint], "cached": True,
                                  "hint": "相同参数的结果已复用。若不足请改写查询或换页面，不要重复调用。"}
                    elif attempts.get(fingerprint, 0) >= 2:
                        result = {"ok": False, "error": "repeated_tool_call", "retryable": False,
                                  "hint": "同参数瞬时故障已经尝试两次，请换方案或说明限制。"}
                    else:
                        attempts[fingerprint] = attempts.get(fingerprint, 0) + 1
                        try:
                            result = await asyncio.wait_for(
                                handlers[name](args),
                                timeout=min(tool_timeout, remaining() - final_reserve),
                            )
                        except asyncio.TimeoutError:
                            result = {"ok": False, "error": "tool_timeout", "retryable": True}
                        except ProviderError as error:
                            result = {"ok": False, "error": error.code, "retryable": False}
                        except Exception as error:
                            logger.error("tool_failed tool=%s exception_type=%s", name, type(error).__name__)
                            result = {"ok": False, "error": "tool_internal", "retryable": False}
                        if not isinstance(result, dict):
                            result = {"ok": False, "error": "invalid_tool_result", "retryable": False}
                        # 成功和确定性失败复用；瞬时失败最多让模型选择再尝试一次。
                        # 来源核验/候选出现会改变本轮状态，不能缓存此前的状态拒绝。
                        state_dependent = name == "verify_source" or result.get("error") in {
                            "source_unverified", "government_page_not_read", "unknown_candidate",
                        }
                        if not state_dependent and (result.get("ok") or not result.get("retryable", False)):
                            cached[fingerprint] = result
            try:
                payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                result = {"ok": False, "error": "invalid_tool_result", "retryable": False}
                payload = json.dumps(result)
            if len(payload) > 20000:
                result = {"ok": False, "error": "tool_result_too_large", "retryable": False}
                payload = json.dumps(result)
            await emit("agent_tool", status="ok" if result.get("ok") else "failed",
                       tool=name, tool_call_id=call["id"], error_code=result.get("error"),
                       step=step,
                       source_count=len(result.get("sources", [])),
                       candidate_count=len(result.get("candidates", [])),
                       outcome=result.get("outcome"), http_status=result.get("http_status"),
                       cached=bool(result.get("cached", False)),
                       reader=result.get("reader"), authority=result.get("authority"),
                       document_type=result.get("document_type"),
                       provider_attempt_count=len(result.get("provider_attempts", [])))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": payload})
    raise ToolLoopError("Agent did not finish")
