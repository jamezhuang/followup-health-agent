import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.planned_search import (
    PlannedKnowledgeSearch, QueryPlan, SearchCatalog, SearchTask,
    matches_scope, select_balanced, validate_plan,
)
from app.providers import ProviderError
from app.schemas import Source


HOSPITALS = [{"id": "H1", "name": "甲医院", "aliases": ["甲院"]},
             {"id": "H2", "name": "乙医院", "aliases": ["乙院"]}]


def source(identifier, text_value="公开资料正文"):
    return Source(id=identifier, title=identifier, institution="测试机构",
                  url="https://example.com/source", published_at=None,
                  collected_at="2026-10-07", text=text_value,
                  allowed_departments=[], kind="official_summary")


def task(scope="general", query="一般原理", hospital_id=None, hospital_name=None):
    return SearchTask(scope=scope, query=query, hospital_id=hospital_id, hospital_name=hospital_name)


def detail(items):
    return {"dense": items, "keyword": items, "results": items,
            "mode": "hybrid", "warnings": []}


class FakePlanner:
    def __init__(self, plan=None, error=None):
        self.plan, self.error, self.inputs = plan, error, []

    async def structured(self, schema, prompt, payload):
        self.inputs.append(payload)
        if self.error:
            raise self.error
        return self.plan


class FakeCatalog:
    def __init__(self, records):
        self.records = records

    def hospitals(self):
        return HOSPITALS

    def metadata(self, sources):
        return {item.id: self.records[item.id] for item in sources if item.id in self.records}


class FakeSearch(PlannedKnowledgeSearch):
    def __init__(self, planner, raw, records=None):
        self._plan_settings = SimpleNamespace(mode="live")
        self._plan_client = planner
        self._catalog = FakeCatalog(records or {})
        self._planning_enabled = True
        self.candidates = 10
        self.raw, self.queries = raw, []

    async def _raw_search(self, query, top_k):
        self.queries.append(query)
        result = self.raw[query]
        if isinstance(result, Exception):
            raise result
        return result


def test_general_keeps_education_from_other_hospital():
    assert matches_scope(task(), {"category": "health_general", "hospital_id": "H2"})
    assert not matches_scope(task(), {"category": "hospital_department", "hospital_id": "H2"})


def test_hospital_filter_requires_category_and_target():
    target = task("hospital", "科室", "H1", "甲医院")
    assert matches_scope(target, {"category": "hospital_department", "hospital_id": "H1"})
    assert not matches_scope(target, {"category": "hospital_department", "hospital_id": "H2"})
    assert not matches_scope(target, {"category": "health_general", "hospital_id": "H1"})


def test_balanced_selection_retains_each_task_and_deduplicates():
    a, b, c, d = [source(key) for key in "ABCD"]
    assert [item.id for item in select_balanced([[a, b, c], [a, d]], 3)] == ["A", "D", "B"]


@pytest.mark.parametrize("topic", ["痛风机制", "流泪与干眼"])
def test_composite_retains_general_and_requested_hospital(topic):
    query = f"{topic}，甲医院的相关科室？"
    plan = QueryPlan(tasks=[task(query=topic), task("hospital", "目标科室", "H1", "甲医院")])
    public, target, other, wrong_type = [source(key) for key in ("PUBLIC", "TARGET", "OTHER", "WRONG-TYPE")]
    records = {
        "PUBLIC": {"category": "health_general", "hospital_id": "H2"},
        "TARGET": {"category": "hospital_department", "hospital_id": "H1"},
        "OTHER": {"category": "hospital_department", "hospital_id": "H2"},
        "WRONG-TYPE": {"category": "hospital_service", "hospital_id": "H2"},
    }
    planner = FakePlanner(plan)
    search = FakeSearch(planner, {topic: detail([target, public]),
                                 "目标科室": detail([other, wrong_type, target])}, records)
    result = asyncio.run(search.search_detailed(query, 3))
    assert [item.id for item in result["results"]] == ["PUBLIC", "TARGET"]
    assert set(planner.inputs[0]) == {"question", "hospitals"}
    assert len(result["branches"]) == 2
    assert "OTHER" not in [item.id for item in result["results"]]
    assert len(asyncio.run(search.search(query, 3))) == 2


def test_unknown_hospital_does_not_retrieve_other_hospitals():
    plan = QueryPlan(tasks=[task("hospital", "科室", None, "未收录医院")])
    search = FakeSearch(FakePlanner(plan), {})
    result = asyncio.run(search.search_detailed("未收录医院的科室？", 3))
    assert result["results"] == []
    assert search.queries == []
    assert "target_hospital_not_cataloged" in result["warnings"][0]


def test_planner_failure_falls_back_explicitly():
    query = "一般问题"
    search = FakeSearch(FakePlanner(error=ProviderError("timeout", "超时")), {query: detail([source("A")])})
    result = asyncio.run(search.search_detailed(query))
    assert result["strategy"] == "baseline_fallback"
    assert result["warnings"] == ["planner: timeout"]


def test_invalid_target_cannot_be_used():
    plan = QueryPlan(tasks=[task("hospital", "科室", "H2", "乙医院")])
    with pytest.raises(ProviderError, match="不一致"):
        validate_plan(plan, "甲医院的科室", HOSPITALS)


def test_plan_removes_publisher_name_from_general_query():
    plan = QueryPlan(tasks=[task(query="甲医院 痛风科普")])
    result = validate_plan(plan, "甲医院的痛风科普", HOSPITALS)
    assert result.tasks[0].query == "痛风科普"


def test_one_branch_failure_keeps_other_branch_and_reports_warning():
    plan = QueryPlan(tasks=[task(query="科普"), task("hospital", "科室", "H1", "甲医院")])
    search = FakeSearch(FakePlanner(plan), {"科普": ProviderError("timeout", "超时"),
                                           "科室": detail([source("A")])},
                        {"A": {"category": "hospital_department", "hospital_id": "H1"}})
    result = asyncio.run(search.search_detailed("甲医院科室及科普"))
    assert [item.id for item in result["results"]] == ["A"]
    assert any("branch_failed" in warning for warning in result["warnings"])


def test_all_branches_failure_raises():
    plan = QueryPlan(tasks=[task(query="科普")])
    search = FakeSearch(FakePlanner(plan), {"科普": ProviderError("timeout", "超时")})
    with pytest.raises(ProviderError):
        asyncio.run(search.search_detailed("科普"))


def test_disabled_planner_uses_original_search():
    planner = FakePlanner()
    search = FakeSearch(planner, {"问题": detail([source("A")])})
    search._planning_enabled = False
    result = asyncio.run(search.search_detailed("问题"))
    assert result["strategy"] == "baseline"
    assert planner.inputs == []


class FakeEngine:
    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, statement, parameters):
        self.sql = str(statement.params(**parameters).compile(
            dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}))
        return self

    def mappings(self):
        return self

    def all(self):
        return []


@pytest.mark.parametrize("identifier", ["KB-" + "a" * 24, "DOCUMENT-ID"])
def test_empty_sql_parameter_lists_are_strings(identifier):
    engine = FakeEngine()
    SearchCatalog(engine).metadata([source(identifier)])
    assert "CAST(NULL AS VARCHAR)" in engine.sql
    assert "CAST(NULL AS INTEGER)" not in engine.sql
