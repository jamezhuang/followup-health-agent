"""Offline tests: fake repository/provider, real tool handlers and Source validation."""
import asyncio
from types import SimpleNamespace
from typing import Literal
import pytest
from pydantic import BaseModel, ValidationError
from app.workspace.private_documents import PrivateRead, read_confirmed
from app.workspace.repository import WorkspaceError
from app.workspace.tools import identity, register_private_tools
from scripts.apply_private_document_fix import patch_source_kind

DOC = "a" * 32
OTHER = "b" * 32


class Repo:
    def __init__(self):
        self.selected_ids = [DOC]
        self.read_calls = []
        self.text = "# 症状\n自述头晕。\n" + "甲" * 2400 + "\n后续补充：持续三天。"
        self.docs = {DOC: {"id":DOC, "name":"breast-report.md", "owner":"alice", "status":"ready", "revision":2,
                           "pieces":[{"location":"段落1", "text":self.text}], "chunk_count":0},
                     OTHER:{"id":OTHER, "name":"other.md", "owner":"bob", "status":"ready", "revision":2,
                            "pieces":[{"location":"段落1", "text":"另一账号隐私"}]}}

    def document(self,owner,document_id):
        self.read_calls.append((owner,document_id))
        d=self.docs.get(document_id)
        if not d or d["owner"] != owner or d["status"]=="deleted":
            raise WorkspaceError("document_not_found",404)
        return d

    def documents(self,owner):
        return [{"id":d["id"],"name":d["name"],"status":d["status"],"error_code":""}
                for d in self.docs.values() if d["owner"]==owner and d["status"]!="deleted"]

    def selected(self,owner,session):
        return self.selected_ids

    def search(self,*args):
        return []


class Embeddings:
    def __init__(self,fail=False):
        self.calls=0
        self.fail=fail
    async def embed(self,text):
        self.calls+=1
        if self.fail:
            raise RuntimeError("private-key-should-not-log")
        return [[1.0,0.0]]


class Events:
    def __init__(self):self.events=[]
    async def write(self,run_id,stage,**details):self.events.append({"stage":stage,**details})


@pytest.fixture
def repo():return Repo()


def execute(repo,tool,args,message="",fail=False):
    async def scenario():
        embeddings,events=Embeddings(fail),Events()
        agent=SimpleNamespace(workspace_documents=SimpleNamespace(repo=repo,client=embeddings),events=events)
        tools,handlers,collected=[],{},{}
        token=identity.set(("alice","session"))
        try:
            register_private_tools(agent,tools,handlers,collected,{"run_id":"run","message":message})
            result=await handlers[tool](args)
            return result,collected,embeddings,events,tools
        finally:identity.reset(token)
    return asyncio.run(scenario())


def test_ready_old_md_without_vector_chunks_is_readable(repo):
    page=read_confirmed(repo,"alice",DOC)
    assert page["rows"][0]["content"].startswith("# 症状")
    assert page["reader"]=="confirmed_text" and page["total_fragments"]==3
    assert page["next_offset"] is None


def test_pagination_quote_and_revision(repo):
    page=read_confirmed(repo,"alice",DOC,0,1)
    assert page["has_more"] and page["next_offset"]==1
    second=read_confirmed(repo,"alice",DOC,1,1)
    third=read_confirmed(repo,"alice",DOC,2,1)
    assert "持续三天" in third["rows"][0]["content"]
    assert page["rows"][0]["content"]+second["rows"][0]["content"]+third["rows"][0]["content"]==repo.text
    assert "-2-READ-" in page["rows"][0]["id"]


@pytest.mark.parametrize("status",["awaiting_confirmation","indexing","failed","deleted"])
def test_nonready_or_deleted_never_read(repo,status):
    repo.docs[DOC]["status"]=status
    with pytest.raises(WorkspaceError):read_confirmed(repo,"alice",DOC)


def test_cross_owner_rejected(repo):
    with pytest.raises(WorkspaceError) as exc:read_confirmed(repo,"alice",OTHER)
    assert exc.value.code=="document_not_found"


@pytest.mark.parametrize("raw",[{"document_id":DOC,"owner":"bob"},{"document_id":DOC,"user_id":"bob"},
                               {"document_id":"../file"},{"document_id":DOC,"limit":4},
                               {"document_id":DOC,"offset":-1}])
def test_read_parameters_reject_unauthorized_scope(raw):
    with pytest.raises(ValidationError):PrivateRead.model_validate(raw)


def test_read_tool_collects_real_sources_without_embedding(repo):
    result,collected,embeddings,events,tools=execute(repo,"read_user_document",{"document_id":DOC,"limit":1})
    assert result["ok"] and collected and embeddings.calls==0
    source=next(iter(collected.values()))
    assert source.kind=="private_document" and source.text in repo.text
    assert source.url==f"/workspace/documents/{DOC}/file"
    assert result["has_more"] and result["next_offset"]==1
    assert any(t["function"]["name"]=="read_user_document" for t in tools)


def test_selection_restricts_direct_reader(repo):
    repo.docs[OTHER]["owner"]="alice"
    result,*_=execute(repo,"read_user_document",{"document_id":OTHER})
    assert not result["ok"] and result["error"]=="document_outside_selection"
    assert not repo.read_calls


def test_no_selection_can_read_only_owned_files(repo):
    repo.selected_ids=[]
    result,*_=execute(repo,"read_user_document",{"document_id":DOC})
    assert result["ok"]
    other,*_=execute(repo,"read_user_document",{"document_id":OTHER})
    assert not other["ok"] and other["error"]=="document_not_found"


def test_named_md_search_uses_direct_text(repo):
    result,collected,embeddings,*_=execute(repo,"search_user_documents",{"query":"我的症状"},
                                        message="读取breast-report.md，整理我的症状",fail=True)
    assert result["ok"] and result["reader"]=="confirmed_text"
    assert collected and embeddings.calls==0


def test_embedding_failure_with_selection_recovers(repo,caplog):
    result,collected,embeddings,events,_=execute(repo,"search_user_documents",{"query":"这个指标"},fail=True)
    assert result["ok"] and result["reader"]=="confirmed_text_fallback" and collected
    assert embeddings.calls==1 and any(e.get("status")=="failed" for e in events.events)
    assert "private-key-should-not-log" not in caplog.text
    assert repo.text not in caplog.text


def test_embedding_failure_without_selection_is_actionable(repo):
    repo.selected_ids=[]
    result,collected,*_=execute(repo,"search_user_documents",{"query":"这个指标"},fail=True)
    assert not result["ok"] and not collected and result["failure_stage"]=="embedding"
    assert "read_user_document" in result["hint"]


def test_missing_private_kind_has_distinct_error(repo,monkeypatch):
    import app.schemas
    class OldSource(BaseModel):
        kind:Literal["official_summary","external_excerpt"]
    monkeypatch.setattr(app.schemas,"Source",OldSource)
    result,collected,*_=execute(repo,"read_user_document",{"document_id":DOC})
    assert not result["ok"] and result["error"]=="private_source_schema_invalid"
    assert not collected and result["failure_stage"]=="source_schema"


def test_empty_document_does_not_claim_read_success(repo):
    repo.docs[DOC]["pieces"]=[]
    result,*_=execute(repo,"read_user_document",{"document_id":DOC})
    assert not result["ok"] and result["error"]=="private_document_empty"


def test_no_identity_means_no_private_tools(repo):
    token=identity.set(None)
    try:
        tools,handlers,collected=[],{},{}
        register_private_tools(SimpleNamespace(workspace_documents=SimpleNamespace(repo=repo)),tools,handlers,collected,{})
        assert not tools and not handlers
    finally:identity.reset(token)


def test_kind_installer_preserves_other_model():
    original='from typing import Literal\nclass Source:\n    kind: Literal["official_summary", "external_excerpt", "custom"]\nclass Answer:\n    summary: str\n'
    revised=patch_source_kind(original)
    assert 'private_document' in revised and "'custom'" in revised
    assert "class Answer:\n    summary: str" in revised
    assert patch_source_kind(revised)==revised


def test_kind_installer_rejects_unknown_annotation():
    with pytest.raises(ValueError):patch_source_kind('class Source:\n    kind: str\n')
