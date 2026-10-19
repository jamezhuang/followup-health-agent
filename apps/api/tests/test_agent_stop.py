import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agent_stop import StopRegistry, install_agent_stop
from app.providers import ProviderError

@pytest.fixture
def anyio_backend():
    return "asyncio"

REQ = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NEXT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class Store:
    def user(self, cookie):
        return {"id": cookie} if cookie in {"alice", "bob"} else None

    def session(self, session_id, owner):
        return {"id": session_id} if session_id == owner + "-chat" else None


class Events:
    def __init__(self):
        self.rows = []

    async def write(self, run_id, stage, **details):
        self.rows.append({"run_id": run_id, "stage": stage, **details})


class Agent:
    def __init__(self):
        self.events = Events()
        self.started, self.cleaned = asyncio.Event(), asyncio.Event()
        self.block = True
        self.swallow = False
        self.entered = 0

    async def run(self, message, history, facts, run_id):
        self.entered += 1
        self.started.set()
        try:
            if self.block:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not self.swallow:
                raise
        finally:
            self.cleaned.set()
        return {"message": message, "run_id": run_id}


def app_for():
    app = FastAPI()
    app.state.agent = Agent()
    app.state.store = Store()
    app.state.committed = []
    lock = asyncio.Lock()

    @app.post("/sessions/{session_id}/messages")
    async def message(session_id: str, request: Request):
        body = await request.json()
        async with lock:
            try:
                result = await app.state.agent.run(body["message"], [], {}, "run-test")
            except ProviderError as error:
                return JSONResponse(status_code=503, content={"code": error.code, "run_id": "run-test"})
            app.state.committed.append(result)
            return result

    install_agent_stop(app)
    return app


@pytest.mark.anyio
async def test_stop_mid_run_does_not_commit_and_releases_lock():
    app = app_for()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     cookies={"carebrief_login": "alice"}) as client:
            post = asyncio.create_task(client.post(f"/sessions/alice-chat/messages?request_id={REQ}", json={"message": "original"}))
            await app.state.agent.started.wait()
            stopped = await client.post(f"/sessions/alice-chat/requests/{REQ}/cancel")
            response = await post
            assert stopped.json()["status"] == "cancelled"
            assert response.json()["code"] == "request_cancelled"
            assert app.state.agent.cleaned.is_set()
            assert app.state.committed == []
            assert any(e["stage"] == "agent_cancel" for e in app.state.agent.events.rows)
            app.state.agent.block = False
            next_result = await client.post(f"/sessions/alice-chat/messages?request_id={NEXT}", json={"message": "next"})
            assert next_result.status_code == 200
            assert len(app.state.committed) == 1


@pytest.mark.anyio
async def test_immediate_stop_before_post_prevents_model_call():
    app = app_for()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     cookies={"carebrief_login": "alice"}) as client:
            assert (await client.post(f"/sessions/alice-chat/requests/{REQ}/cancel")).json()["status"] == "cancelled"
            response = await client.post(f"/sessions/alice-chat/messages?request_id={REQ}", json={"message": "oops"})
            assert response.status_code == 409
            assert response.json()["code"] == "request_cancelled"
            assert app.state.agent.entered == 0


@pytest.mark.anyio
async def test_other_owner_cannot_cancel():
    app = app_for()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     cookies={"carebrief_login": "bob"}) as client:
            response = await client.post(f"/sessions/alice-chat/requests/{REQ}/cancel")
            assert response.status_code == 404
            assert app.state.agent_stop_registry.entries == {}


@pytest.mark.anyio
async def test_finished_response_is_not_revoked():
    app = app_for()
    app.state.agent.block = False
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     cookies={"carebrief_login": "alice"}) as client:
            response = await client.post(f"/sessions/alice-chat/messages?request_id={REQ}", json={"message": "done"})
            assert response.status_code == 200
            stopped = await client.post(f"/sessions/alice-chat/requests/{REQ}/cancel")
            assert stopped.json()["status"] == "already_finished"
            assert len(app.state.committed) == 1
            duplicate = await client.post(f"/sessions/alice-chat/messages?request_id={REQ}", json={"message": "duplicate"})
            assert duplicate.status_code == 409
            assert len(app.state.committed) == 1


@pytest.mark.anyio
async def test_swallowed_cancellation_cannot_publish_result():
    app = app_for()
    app.state.agent.swallow = True
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     cookies={"carebrief_login": "alice"}) as client:
            post = asyncio.create_task(client.post(f"/sessions/alice-chat/messages?request_id={REQ}", json={"message": "oops"}))
            await app.state.agent.started.wait()
            assert (await client.post(f"/sessions/alice-chat/requests/{REQ}/cancel")).json()["status"] == "cancelled"
            assert (await post).json()["code"] == "request_cancelled"
            assert not app.state.committed


@pytest.mark.anyio
async def test_untracked_legacy_message_still_works():
    app = app_for()
    app.state.agent.block = False
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/sessions/alice-chat/messages", json={"message": "legacy"})).status_code == 200
            assert app.state.agent_stop_registry.entries == {}


@pytest.mark.anyio
async def test_login_and_request_id_validation():
    app = app_for()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post(f"/sessions/alice-chat/requests/{REQ}/cancel")).status_code == 401
            assert (await client.post("/sessions/alice-chat/messages?request_id=bad",json={"message":"x"})).status_code == 422


@pytest.mark.anyio
async def test_slow_cleanup_returns_stopping_and_repeated_stop_does_not_recancel():
    registry = StopRegistry()
    entry = registry.reserve(("a", "s", REQ))
    cleanup = asyncio.Event()

    async def task():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await cleanup.wait()
        finally:
            entry.phase = "cancelled"
            entry.settled.set()

    entry.phase = "running"
    entry.task = asyncio.create_task(task())
    await asyncio.sleep(0)
    assert (await registry.cancel(entry.key, timeout=0.01))["status"] == "stopping"
    assert (await registry.cancel(entry.key, timeout=0.01))["status"] == "stopping"
    assert entry.task.cancelling() == 1
    cleanup.set()
    await entry.task
    assert (await registry.cancel(entry.key))["status"] == "cancelled"


@pytest.mark.anyio
async def test_registry_bounds_completed_records_and_isolates_owner():
    registry = StopRegistry(capacity=2)
    await registry.cancel(("a", "s", "1"))
    await registry.cancel(("b", "s", "1"))
    assert len(registry.entries) == 2
    assert registry.get(("c", "s", "1")) is None
    await registry.cancel(("c", "s", "2"))
    assert len(registry.entries) == 2


def test_installer_is_idempotent_and_preserves_original_calls():
    path = Path(__file__).resolve().parents[1] / "scripts/install_agent_stop.py"
    spec = importlib.util.spec_from_file_location("stop_patcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = "def install_workspace(app):\n    if app.state.installed:\n        return\n    app.include_router(build_router())\n"
    result = module.patch(original)
    assert module.patch(result) == result
    assert original.rstrip() in result
    assert result.count("install_agent_stop(app)") == 1


def test_installer_marks_workspace_cancelled_without_changing_errors():
    path = Path(__file__).resolve().parents[1] / "scripts/install_agent_stop.py"
    spec = importlib.util.spec_from_file_location("stop_patcher_status", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = '\n'.join([
        'def attach_agent(agent, documents):',
        '    async def run():',
        '        try:',
        '            await agent.run()',
        '        except asyncio.CancelledError:',
        '            status, code = "failed", "request_cancelled"',
        '            raise',
        '        except Exception:',
        '            status, code = "failed", "internal"',
        '            raise',
        'def install_workspace(app):',
        '    app.include_router(build_router())',
        '',
    ])
    result = module.patch(original)
    assert 'status, code = "cancelled", "request_cancelled"' in result
    assert 'status, code = "failed", "internal"' in result
    assert module.patch(result) == result
