import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.providers import APIClient, ProviderError
from app.retrieval import KnowledgeSearch
from app.safety import explicit_risks


@pytest.fixture
def settings(tmp_path):
    s = Settings(
        mode="demo",
        dev_token="test-dev-token",
        deepseek_key="",
        embedding_key="",
        bocha_key="",
        external_enabled=False,
        cookie_secure=False,
        ocr_enabled=False,
    )
    s.rag_backend = "json"

    s.index_path = tmp_path / "vectors.json"
    s.log_path = tmp_path / "events.jsonl"
    s.database_url = "sqlite:///" + str(tmp_path / "test.db")

    return s


def session(client):
    if (
        client.get("/health").json()["provider_mode"] == "live"
        and not client.get("/auth/me").json()["user"]
    ):
        assert (
            client.post(
                "/auth/register",
                json={"email": "testuser01", "password": "Testpassword123"},
            ).status_code
            == 201
        )
    r = client.post("/sessions")
    assert r.status_code == 201
    return r.json()["id"]


def send(client, id, text):
    return client.post(f"/sessions/{id}/messages", json={"message": text})


def test_demo_multiturn(settings):
    with TestClient(create_app(settings)) as client:
        id = session(client)
        r = send(client, id, "我28岁，咳嗽3天，想了解就诊方向。")
        assert r.status_code == 200
        result = r.json()["last_result"]
        assert result["provider_mode"] == "demo"
        sources = {s["id"]: s for s in result["sources"]}
        assert sources and result["claims"]
        assert result["department"]
        assert all(
            c["quote"] in sources[c["evidence_id"]]["text"] for c in result["claims"]
        )
        assert len(result["follow_up_questions"]) == 2
        r = send(client, id, "没有发烧，没有呼吸困难，没有哮喘病史，不抽烟。")
        assert r.status_code == 200
        assert r.json()["facts"]["age"] == "28岁"
        assert r.json()["facts"]["duration"] == "3天"
        assert not r.json()["last_result"]["risk_notice"]
        assert r.json()["last_result"]["missing_information"] == []
        assert len(r.json()["messages"]) == 5


@pytest.mark.parametrize(
    "text,expected",
    [
        ("现在呼吸困难", ["呼吸困难"]),
        ("没有呼吸困难，咳嗽3天", []),
        ("如果有呼吸困难怎么办", []),
        ("去年呼吸困难", []),
        ("以前没有，但现在呼吸困难", ["呼吸困难"]),
        ("不是没有呼吸困难", ["呼吸困难"]),
        ("没有呼吸困难，但现在咳血", ["咳血"]),
    ],
)
def test_risk_matcher(text, expected):
    assert explicit_risks(text) == expected


def test_risk_skips_retrieval_keeps_data_and_dev_auth(settings):
    app = create_app(settings)

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not retrieve")

    app.state.agent.search.search = forbidden
    with TestClient(app) as client:
        id = session(client)
        r = send(client, id, "我28岁，咳嗽3天，现在喘不过气")
        assert r.status_code == 200
        result = r.json()["last_result"]
        assert result["risk_notice"] and not result["sources"]
        assert r.json()["facts"]["duration"] == "3天"
        assert client.get(f"/sessions/{id}").status_code == 200
        assert client.get(f'/dev/runs/{result["run_id"]}').status_code == 401
        app.state.store.register("logadmin01", "Admin123", role="admin")
        assert client.post("/auth/login", json={"username":"logadmin01", "password":"Admin123"}).status_code == 200
        assert client.get(f'/dev/runs/{result["run_id"]}').status_code == 403
        events = client.get(
            f'/dev/runs/{result["run_id"]}', headers={"X-Dev-Token": "test-dev-token"}
        ).json()["events"]
        assert "boundary" in [e["stage"] for e in events]
        assert "喘不过气" not in settings.log_path.read_text(encoding="utf-8")


def test_live_risk_gate_precedes_missing_keys(settings):
    settings.mode = "live"
    settings.deepseek_key = settings.embedding_key = ""
    with TestClient(create_app(settings)) as client:
        r = send(client, session(client), "现在呼吸困难")
        assert r.status_code == 200
        assert r.json()["last_result"]["risk_notice"]


@pytest.mark.parametrize(
    "text", ["咳嗽应该吃什么药", "帮我看化验单报告", "我12岁咳嗽3天", "我头痛"]
)
def test_scope(settings, text):
    with TestClient(create_app(settings)) as client:
        r = send(client, session(client), text).json()["last_result"]
        assert r["department"] == "" and not r["sources"] and not r["claims"]


def test_topic_change(settings):
    with TestClient(create_app(settings)) as client:
        id = session(client)
        send(client, id, "我28岁咳嗽3天")
        assert not send(client, id, "这次想咨询头痛").json()["last_result"]["sources"]


def test_api_validation_delete(settings):
    with TestClient(create_app(settings)) as client:
        id = session(client)
        assert send(client, id, "   ").status_code == 422
        assert send(client, id, "a" * 2001).status_code == 422
        assert client.get("/sessions/unknown").status_code == 404
        assert client.delete(f"/sessions/{id}").status_code == 204
        assert client.get(f"/sessions/{id}").status_code == 404


def transport_fixture(docs, bad_tool=False, bad_citation=False, bad_verify=False):
    calls = []

    def handle(request):
        b = json.loads(request.content)
        calls.append((request.url.path, b))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [1.0] + [0.0] * 63}
                        for i, _ in enumerate(b["input"])
                    ]
                },
            )
        if (
            "tools" in b
            and b.get("tool_choice") != "none"
            and not any(m.get("role") == "tool" for m in b["messages"])
        ):
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": (
                                "delete_database" if bad_tool else "search_knowledge"
                            ),
                            "arguments": json.dumps(
                                {"query": "咳嗽 呼吸 就医", "top_k": 3}
                            ),
                        },
                    }
                ],
            }
        else:
            prompt = b["messages"][0]["content"]
            if '"title": "Classification"' in prompt:
                payload = {
                    "intent": "department",
                    "supported": True,
                    "observations": [
                        {"name": "age", "value": "28岁", "evidence": "28岁"},
                        {"name": "symptom", "value": "咳嗽", "evidence": "咳嗽"},
                        {"name": "duration", "value": "3天", "evidence": "3天"},
                    ],
                }
            elif '"title": "Verification"' in prompt:
                payload = {"supported": not bad_verify, "reason": "fixture"}
            else:
                d = docs[1]
                payload = {
                    "summary": "已整理输入，科室信息可向当地导诊核实。",
                    "department": d["allowed_departments"][0],
                    "claims": [
                        {
                            "text": d["text"],
                            "evidence_id": "FAKE" if bad_citation else d["id"],
                            "quote": d["text"],
                        }
                    ],
                }
            msg = {
                "role": "assistant",
                "content": json.dumps(payload, ensure_ascii=False),
            }
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": msg}]}
        )

    return httpx.MockTransport(handle), calls


async def live(settings, **options):
    settings.mode = "live"
    settings.dimensions = 64
    settings.deepseek_key = settings.embedding_key = "fake-secret"
    docs = json.loads(settings.knowledge_path.read_text(encoding="utf-8"))
    transport, calls = transport_fixture(docs, **options)
    provider = APIClient(settings, transport)
    search = KnowledgeSearch(settings, provider)
    assert await search.build_index()
    count = len(calls)
    assert not await search.build_index()
    assert count == len(calls)
    return provider, calls


@pytest.mark.asyncio
async def test_live_contract(settings):
    provider, calls = await live(settings)
    with TestClient(create_app(settings, provider)) as client:
        r = send(client, session(client), "我28岁，咳嗽3天，想挂什么科")
        assert r.status_code == 200
        result = r.json()["last_result"]
        assert result["provider_mode"] == "live"
        assert result["claims"][0]["evidence_id"] == "PUMCH-DEPT-01"
        assert len([b for p, b in calls if p.endswith("/embeddings")]) == 2
        assert any("tools" in b for _, b in calls)
        assert "fake-secret" not in settings.log_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option,code",
    [
        ("bad_citation", "invalid_citation"),
        ("bad_verify", "unsupported_answer"),
    ],
)
async def test_invalid_output_rolls_back(settings, option, code):
    provider, _ = await live(settings, **{option: True})
    with TestClient(create_app(settings, provider)) as client:
        id = session(client)
        r = send(client, id, "我28岁，咳嗽3天，想挂什么科")
        assert r.status_code == 503 and r.json()["code"] == code
        saved = client.get(f"/sessions/{id}").json()
        assert (
            saved["facts"] == {}
            and len(saved["messages"]) == 1
            and saved["last_result"] is None
        )
        assert code in settings.log_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_index_fingerprint(settings):
    provider, _ = await live(settings)
    settings.embedding_model = "another-model"
    with pytest.raises(ProviderError, match="过期"):
        await KnowledgeSearch(settings, provider).search("咳嗽")


@pytest.mark.asyncio
async def test_auth_error_not_retried_or_exposed(settings):
    calls = []

    def denied(request):
        calls.append(request)
        return httpx.Response(401, text="secret-key-provider-body")

    provider = APIClient(settings, httpx.MockTransport(denied))
    with pytest.raises(ProviderError) as e:
        await provider.embed(["test"])
    assert e.value.code == "missing_key"
    settings.embedding_key = "fake-secret"
    with pytest.raises(ProviderError) as e:
        await provider.embed(["test"])
    assert len(calls) == 1 and "secret" not in e.value.public_message


@pytest.mark.asyncio
async def test_bad_json_schema_and_zero_vector(settings):
    from app.schemas import Verification

    settings.deepseek_key = settings.embedding_key = "fake"
    provider = APIClient(
        settings,
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"supported": "yes"}'},
                        }
                    ]
                },
            )
        ),
    )
    with pytest.raises(ProviderError) as e:
        await provider.structured(Verification, "check", {})
    assert e.value.code == "invalid_schema"
    provider = APIClient(
        settings,
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.0] * settings.dimensions}]},
            )
        ),
    )
    with pytest.raises(ProviderError) as e:
        await provider.embed(["test"])
    assert e.value.code == "embedding_format"


@pytest.mark.asyncio
async def test_duplicate_submission(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        id = session(client)
        lock = app.state.sessions[id].lock
        await lock.acquire()
        try:
            assert send(client, id, "咳嗽").status_code == 409
        finally:
            lock.release()
