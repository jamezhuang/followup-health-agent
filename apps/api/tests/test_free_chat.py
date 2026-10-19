import copy
import json
import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas import (
    Answer,
    Classification,
    Observation,
    Verification,
    Claim,
    Source,
)
from app.conversation import merge_observations
from test_v0 import settings, session, send


class FakeLLM:
    def __init__(self, answers, observations=None):
        self.answers = iter(answers)
        self.observations = iter(observations or [[], []])
        self.requests = []

    async def structured(self, schema, instruction, data):
        if schema is Classification:
            return Classification(
                intent="symptom",
                supported=False,
                observations=next(self.observations),
            )

        return Verification(
            supported=True,
            reason="test",
        )

    async def complete(self, messages, **options):
        self.requests.append(copy.deepcopy(messages))

        answer = next(self.answers)

        if isinstance(answer, dict):
            return answer

        return {"content": answer.model_dump_json()}

    async def embed(self, texts):
        return [[1.0] + [0.0] * 63 for _ in texts]


def test_free_description_and_short_reply(settings):
    settings.mode = "live"

    llm = FakeLLM(
        [
            Answer(
                summary="可以直接描述，不用填写表格。",
                follow_up_questions=["这些不舒服持续多久了？"],
                missing_information=["持续时间"],
            ),
            Answer(summary="收到你的补充。"),
        ],
        [
            [
                Observation(
                    name="symptom",
                    value="感冒了",
                    evidence="感冒了",
                )
            ],
            [
                Observation(
                    name="duration",
                    value="五天",
                    evidence="五天",
                )
            ],
        ],
    )

    with TestClient(create_app(settings, llm)) as client:
        id = session(client)

        first = send(client, id, "我感冒了")

        assert first.status_code == 200
        assert first.json()["last_result"]["missing_information"] == ["持续时间"]

        second = send(client, id, "五天")

        assert second.status_code == 200
        assert second.json()["facts"]["duration"] == "五天"
        assert second.json()["last_result"]["follow_up_questions"] == []
        assert any(
            "这些不舒服持续多久" in m.get("content", "") for m in llm.requests[1]
        )


def test_append_and_explicit_correction():
    old = {
        "associated": "没有发烧",
        "duration": "五天",
    }

    new = merge_observations(
        "流鼻涕，改成六天",
        old,
        [
            Observation(
                name="associated",
                value="流鼻涕",
                evidence="流鼻涕",
                operation="append",
            ),
            Observation(
                name="duration",
                value="六天",
                evidence="六天",
                operation="set",
            ),
        ],
    )

    assert new["associated"] == "没有发烧；流鼻涕"
    assert new["duration"] == "六天"
    assert old["duration"] == "五天"


def tool_call(id, name, query):
    return {
        "tool_calls": [
            {
                "id": id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(
                        {
                            "query": query,
                            "top_k": 2,
                        }
                    ),
                },
            }
        ]
    }


def test_local_then_external_and_citation_in_chat(settings):
    settings.mode = "live"
    settings.dimensions = 64
    settings.external_enabled = True

    web = Source(
        id="WEB-TEST",
        title="模拟健康资料",
        institution="www.pumch.cn",
        url="https://www.pumch.cn/test",
        published_at=None,
        collected_at="2026-10-05",
        text="这是一段模拟健康资料。",
        allowed_departments=[],
        kind="external_excerpt",
    )

    llm = FakeLLM(
        [
            tool_call(
                "one",
                "search_knowledge",
                "一般健康信息",
            ),
            tool_call(
                "two",
                "search_official_web",
                "一般健康信息",
            ),
            Answer(
                summary="已检索两类来源。",
                claims=[
                    Claim(
                        text="检索到模拟健康资料。",
                        evidence_id=web.id,
                        quote=web.text,
                    )
                ],
            ),
        ]
    )

    app = create_app(settings, llm)

    asyncio.run(app.state.agent.search.build_index())

    async def official(query, top_k=3):
        return [web][:top_k]

    app.state.agent.external.search = official

    with TestClient(app) as client:
        result = send(
            client,
            session(client),
            "我感冒了，想了解相关资料",
        )

        assert result.status_code == 200, result.text
        assert "[WEB-TEST]" in result.json()["messages"][-1]["content"]

        ids = [m["tool_call_id"] for m in llm.requests[-1] if m["role"] == "tool"]

        assert ids == ["one", "two"]
