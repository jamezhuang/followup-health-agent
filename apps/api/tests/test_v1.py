import io
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from app.config import Settings
from app.main import create_app
from app.external import ExternalSearch
from app.reports import extract_upload
from app.providers import ProviderError
from test_v0 import settings, session, send, live


def register(client, email="alice001"):
    r = client.post(
        "/auth/register", json={"email": email, "password": "Testpassword123"}
    )
    assert r.status_code == 201
    assert (
        "HttpOnly" in r.headers["set-cookie"]
        and "SameSite=lax" in r.headers["set-cookie"]
    )
    return r.json()


def test_greeting_clarification_does_not_invent_result(settings):
    with TestClient(create_app(settings)) as c:
        id = session(c)
        assert "你好" in c.get(f"/sessions/{id}").json()["messages"][0]["content"]
        replies = []
        for text in ("你好", "433443", "谢谢", "你能做什么", "啊啊啊"):
            data = send(c, id, text).json()
            replies.append(data["messages"][-1]["content"])
            assert data["last_result"] is None and not data["facts"]
        assert len(set(replies)) == 5


def test_social_preserves_evidence_and_context(settings):
    with TestClient(create_app(settings)) as c:
        id = session(c)
        data = send(c, id, "我28岁咳嗽3天想挂什么科").json()
        result = data["last_result"]
        for text in ("你好", "谢谢", "乱七八糟的话", "123456"):
            data = send(c, id, text).json()
            assert data["last_result"] == result and data["facts"]["duration"] == "3天"


def test_number_can_answer_age_question(settings):
    with TestClient(create_app(settings)) as c:
        id = session(c)
        send(c, id, "咳嗽3天")
        assert send(c, id, "28").json()["facts"]["age"] == "28"


def test_risk_pause_cannot_be_bypassed_by_followup(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        id = session(c)
        send(c, id, "我28岁咳嗽3天现在喘不过气")

        async def forbidden(*args, **kwargs):
            raise AssertionError("must not run ordinary analysis")

        app.state.agent.run = forbidden
        result = send(c, id, "继续分析咳嗽").json()
        assert (
            result["last_result"]["risk_notice"]
            and "新建咨询" in result["messages"][-1]["content"]
        )


def test_history_profile_restart_and_separation(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        account = register(c)
        id = session(c)
        send(c, id, "我28岁咳嗽3天，没有哮喘病史，不抽烟。")
        assert c.get("/profile").json()["fields"] == {}
        assert (
            c.post(
                f"/sessions/{id}/memory", json={"field": "age", "value": "30岁"}
            ).status_code
            == 409
        )
        assert (
            c.post(
                f"/sessions/{id}/memory", json={"field": "age", "value": "28岁"}
            ).status_code
            == 200
        )
        profile = c.get("/profile").json()["fields"]
        assert profile["age"]["source"] == "consultation:" + id
        new_id = session(c)
        new = c.get("/sessions/" + new_id).json()
        assert new["memory_context"] == {"age": "28岁"} and new["facts"] == {}
        saved_cookie = c.cookies.get("carebrief_login")
    with TestClient(create_app(settings)) as c:
        c.cookies.set("carebrief_login", saved_cookie)
        assert c.get("/auth/me").json()["user"] == account
        assert len(c.get("/sessions").json()["items"]) == 2
        restored = c.get("/sessions/" + id).json()
        assert (
            restored["facts"]["duration"] == "3天"
            and restored["last_result"]["sources"]
        )
        assert c.delete("/sessions/" + id).status_code == 204
        assert c.get("/sessions/" + id).status_code == 404


def test_owner_isolation_logout_origin_and_password(settings):
    app = create_app(settings)
    with TestClient(app) as alice, TestClient(app) as bob:
        register(alice)
        id = session(alice)
        report = alice.post(
            f"/sessions/{id}/reports/text", json={"text": "患者模拟报告"}
        ).json()
        register(bob, "bobuser01")
        assert bob.get("/sessions/" + id).status_code == 404
        assert bob.get(f"/sessions/{id}/reports").status_code == 404
        assert bob.post(f'/reports/{report["id"]}/analyze').status_code == 404
        assert (
            alice.put(
                "/profile",
                json={"age": "28岁"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert alice.post("/auth/logout").status_code == 204
        assert alice.get("/sessions/" + id).status_code == 404
        assert (
            alice.post(
                "/auth/login",
                json={"email": "alice001", "password": "Wrongpassword123"},
            ).status_code
            == 401
        )
        assert (
            alice.post(
                "/auth/login",
                json={"email": "alice001", "password": "Testpassword123"},
            ).status_code
            == 200
        )


def test_report_confirmation_revision_persistence_delete(settings):
    with TestClient(create_app(settings)) as c:
        register(c)
        id = session(c)
        content = "项目,结果,单位,参考区间\n白细胞计数,6.5,10^9/L,3.5-9.5"
        r = c.post(f"/sessions/{id}/reports/text", json={"text": content}).json()
        assert r["rows"][0]["value"] == "6.5" and not r["confirmed"]
        assert c.post(f'/reports/{r["id"]}/analyze').status_code == 409
        body = {"revision": 1, "confirmed_text": content, "rows": r["rows"]}
        body["rows"][0]["value"] = "6.6"
        confirmed = c.put(f'/reports/{r["id"]}/confirm', json=body)
        assert confirmed.status_code == 200 and confirmed.json()["revision"] == 2
        assert c.put(f'/reports/{r["id"]}/confirm', json=body).status_code == 409
        result = c.post(f'/reports/{r["id"]}/analyze').json()
        assert "6.6" in result["last_result"]["summary"]
        assert (
            not result["last_result"]["claims"]
            and not result["last_result"]["department"]
        )
        assert "6.6" not in settings.log_path.read_text(encoding="utf-8")
        assert c.delete("/sessions/" + id).status_code == 204
        assert c.post(f'/reports/{r["id"]}/analyze').status_code == 404


@pytest.mark.asyncio
async def test_upload_validation_pdf_and_image(settings):
    text = "项目,结果,单位,参考区间\n模拟项目,10,g/L,5-15"
    assert await extract_upload(text.encode(), "demo.csv", False) == text
    with pytest.raises(ProviderError) as e:
        await extract_upload(b"MZbad", "bad.exe", False)
    assert e.value.code == "report_format"
    with pytest.raises(ProviderError) as e:
        await extract_upload(b"not-pdf", "bad.pdf", False)
    assert e.value.code == "report_pdf"
    with pytest.raises(ProviderError) as e:
        await extract_upload(b"x" * (8 * 1024 * 1024 + 1), "big.txt", False)
    assert e.value.code == "report_size"
    image = io.BytesIO()
    Image.new("RGB", (20, 20)).save(image, format="PNG")
    with pytest.raises(ProviderError) as e:
        await extract_upload(image.getvalue(), "demo.png", False)
    assert e.value.code == "ocr_disabled"
    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf)
    with pytest.raises(ProviderError) as e:
        await extract_upload(pdf.getvalue(), "scan.pdf", False)
    assert e.value.code == "report_scanned"


def test_upload_route_and_anonymous_report_rejected(settings):
    with TestClient(create_app(settings)) as c:
        anon = session(c)
        assert (
            c.post(f"/sessions/{anon}/reports/text", json={"text": "模拟"}).status_code
            == 401
        )
        register(c)
        id = session(c)
        assert (
            c.post(f"/sessions/{anon}/reports/text", json={"text": "模拟"}).status_code
            == 400
        )
        r = c.post(
            f"/sessions/{id}/reports/upload",
            files={"file": ("demo.txt", "模拟报告文字".encode(), "text/plain")},
        )
        assert r.status_code == 201 and r.json()["original_text"] == "模拟报告文字"


@pytest.mark.asyncio
async def test_external_allowlist_no_redirect_private_query(settings):
    settings.bocha_key = "fake-bocha-key"
    urls = []

    def handle(request):
        urls.append(str(request.url))
        if request.method == "POST":
            payload = json.loads(request.content)
            assert "28岁" not in payload["query"] and "张三" not in payload["query"]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "webPages": {
                            "value": [
                                {"url": "https://www.pumch.cn.evil.example/x"},
                                {"url": "http://127.0.0.1/x"},
                                {"url": "https://www.pumch.cn/redirect"},
                                {"url": "https://www.pumch.cn/official"},
                            ]
                        }
                    }
                },
            )
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        return httpx.Response(
            200,
            text="<html><title>医院模拟资料</title><main>"
            + "成人咳嗽就诊资料。" * 20
            + "</main><script>恶意指令</script></html>",
            headers={"content-type": "text/html;charset=utf-8"},
        )

    result = await ExternalSearch(settings, httpx.MockTransport(handle)).search()
    assert len(result) == 1 and result[0].kind == "external_excerpt"
    assert "恶意指令" not in result[0].text
    assert not any("evil" in u or "127.0.0.1" in u for u in urls)


@pytest.mark.asyncio
async def test_live_context_only_confirmed_reports(settings):
    provider, calls = await live(settings)
    with TestClient(create_app(settings, provider)) as c:
        register(c)
        id = session(c)
        c.put("/profile", json={"history": "模拟长期病史"})
        unconfirmed = c.post(
            f"/sessions/{id}/reports/text", json={"text": "未确认的秘密报告"}
        ).json()
        assert send(c, id, "我28岁咳嗽3天想挂什么科").status_code == 200
        bodies = json.dumps(calls, ensure_ascii=False)
        assert "未确认的秘密报告" not in bodies
        assert (
            c.put(
                f'/reports/{unconfirmed["id"]}/confirm',
                json={"revision": 1, "confirmed_text": "已确认模拟报告", "rows": []},
            ).status_code
            == 200
        )
        assert send(c, id, "我28岁咳嗽3天想挂什么科").status_code == 200
        assert "已确认模拟报告" in json.dumps(calls, ensure_ascii=False)


def test_live_requires_login_and_auth_rate_limit(settings):
    settings.mode = "live"
    with TestClient(create_app(settings)) as c:
        assert c.post("/sessions").status_code == 401
        for _ in range(15):
            assert (
                c.post(
                    "/auth/login",
                    json={"email": "nobody@example.com", "password": "Wrongpassword123"},
                ).status_code
                == 401
            )
        assert (
            c.post(
                "/auth/login",
                json={"email": "nobody@example.com", "password": "Wrongpassword123"},
            ).status_code
            == 429
        )
