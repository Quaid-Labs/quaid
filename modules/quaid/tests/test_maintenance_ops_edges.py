import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb import maintenance_ops


def test_batch_extract_edges_returns_empty_when_llm_returns_no_edges():
    facts = [{"id": "fact-1", "text": "Diana has a daughter named Alice"}]
    metrics = maintenance_ops.JanitorMetrics()

    with patch.object(
        maintenance_ops,
        "call_deep_reasoning",
        return_value=('[{"fact": 1, "edges": []}]', 0.05),
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert len(results) == 1
    # Policy contract: no deterministic lexical/regex fallback in maintenance_ops.
    # Empty LLM edge output must remain empty; quality fixes go to prompt/planner.
    assert len(results[0]) == 0


def test_batch_extract_edges_uses_llm_edges_for_kinship_pattern():
    facts = [{"id": "fact-2", "text": "Diana has a daughter named Alice", "owner_id": "default"}]
    metrics = maintenance_ops.JanitorMetrics()
    response = (
        '[{"fact": 1, "edges": ['
        '{"subject":"Diana","subject_type":"Person","relation":"parent_of","object":"Alice","object_type":"Person"}'
        ']}]',
        0.05,
    )

    with patch.object(maintenance_ops, "call_deep_reasoning", return_value=response), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert len(results) == 1
    assert len(results[0]) == 1
    edge = results[0][0]
    assert edge["subject"] == "Diana"
    assert edge["relation"] == "parent_of"
    assert edge["object"] == "Alice"


def test_batch_extract_edges_uses_llm_owner_endpoint_from_prompt_guidance():
    facts = [{"id": "fact-4", "text": "David is the user's brother.", "owner_id": "default"}]
    metrics = maintenance_ops.JanitorMetrics()

    response = (
        '[{"fact": 1, "edges": ['
        '{"subject":"David","subject_type":"Person","relation":"sibling_of","object":"Solomon Steadman","object_type":"Person"}'
        ']}]',
        0.05,
    )

    with patch.object(maintenance_ops, "call_deep_reasoning", return_value=response), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert len(results) == 1
    assert len(results[0]) == 1
    edge = results[0][0]
    assert edge["subject"] == "David"
    assert edge["relation"] == "sibling_of"
    assert edge["object"] == "Solomon Steadman"


def test_batch_extract_edges_resolves_owner_once_per_fact_not_per_edge():
    facts = [{"id": "fact-5", "text": "David is the user's brother and Lisa is the user's spouse.", "owner_id": "default"}]
    metrics = maintenance_ops.JanitorMetrics()

    response = (
        '[{"fact": 1, "edges": ['
        '{"subject":"David","subject_type":"Person","relation":"sibling_of","object":"Solomon Steadman","object_type":"Person"},'
        '{"subject":"Lisa","subject_type":"Person","relation":"spouse_of","object":"Solomon Steadman","object_type":"Person"}'
        ']}]',
        0.05,
    )

    with patch.object(maintenance_ops, "call_deep_reasoning", return_value=response), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ) as resolve_owner:
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    # One owner lookup for single-owner prompt guidance, one for the fact itself.
    assert resolve_owner.call_count == 2
    assert len(results) == 1
    assert len(results[0]) == 2


def test_batch_extract_edges_prompt_includes_domain_neutral_role_guardrails():
    facts = [{"id": "fact-6", "text": "Alice is Solomon Steadman's niece"}]
    metrics = maintenance_ops.JanitorMetrics()
    captured = {}

    def _fake_call(prompt: str, max_tokens: int, timeout: float):
        captured["prompt"] = prompt
        return ('[{"fact": 1, "edges": []}]', 0.05)

    with patch.object(maintenance_ops, "call_deep_reasoning", side_effect=_fake_call), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Casey River")
    ):
        maintenance_ops.batch_extract_edges(
            facts=[dict(facts[0], owner_id="casey")],
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of, family_of",
        )

    prompt = captured.get("prompt", "")
    assert "RELATIONSHIP ROLE FIDELITY (MANDATORY)" in prompt
    assert "Do not rewrite one relationship role into a different role family" in prompt
    assert "organizational structure terms" in prompt
    assert "preserve direction exactly as stated" in prompt
    assert "Do not infer hidden intermediate hops unless the intermediate relationship is explicitly stated" in prompt
    assert "use partner_of with alphabetical order" in prompt
    assert "Use family_of only for explicit kinship or relative relationships" in prompt
    assert "Use works_at only for professional employment or ongoing job affiliation stated in the fact" in prompt
    assert "For personal physical training or exercise at an attended venue, use trains_at" in prompt
    assert "gym, studio, dojo, pool" not in prompt
    assert "in any language" in prompt
    assert "first-person, self-reference, possessive owner reference" in prompt
    assert 'owner endpoint is "Casey River"' in prompt
    assert 'emit "Casey River" as that edge endpoint' in prompt
    assert '"the user", "me", "my", "I", or "the owner"' not in prompt


def test_batch_extract_edges_prompt_includes_generic_owner_alias_guidance_without_named_owner():
    metrics = maintenance_ops.JanitorMetrics()
    captured = {}

    def _fake_call(prompt: str, max_tokens: int, timeout: float):
        captured["prompt"] = prompt
        return ('[{"fact": 1, "edges": []}]', 0.05)

    with patch.object(maintenance_ops, "call_deep_reasoning", side_effect=_fake_call):
        maintenance_ops.batch_extract_edges(
            facts=[{"id": "fact-generic-owner", "text": "Leila said my cousin manages the workshop."}],
            graph=object(),
            metrics=metrics,
            relations_list="family_of, manages",
        )

    prompt = captured.get("prompt", "")
    assert 'owner endpoint is "the user"' in prompt
    assert 'emit "the user" as that edge endpoint' in prompt
    assert "in any language" in prompt


def test_batch_extract_edges_accepts_trains_at_relation():
    facts = [
        {
            "id": "fact-6b",
            "text": "Alice Rivera does strength work Tuesday and Friday at Central Training House near her apartment",
            "owner_id": "default",
        }
    ]
    metrics = maintenance_ops.JanitorMetrics()
    response = (
        '[{"fact": 1, "edges": ['
        '{"subject":"Alice Rivera","subject_type":"Person","relation":"trains_at","object":"Central Training House","object_type":"Organization"}'
        ']}]',
        0.05,
    )

    with patch.object(maintenance_ops, "call_deep_reasoning", return_value=response), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Alice Rivera")
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="works_at, trains_at, member_of",
        )

    assert len(results) == 1
    assert results[0][0]["relation"] == "trains_at"
    assert results[0][0]["object"] == "Central Training House"


def test_batch_extract_edges_preserves_professional_employment_as_works_at():
    facts = [
        {
            "id": "fact-6c",
            "text": "Alice Rivera works professionally at Central Training House as an operations manager",
            "owner_id": "default",
        }
    ]
    metrics = maintenance_ops.JanitorMetrics()
    response = (
        '[{"fact": 1, "edges": ['
        '{"subject":"Alice Rivera","subject_type":"Person","relation":"works_at","object":"Central Training House","object_type":"Organization"}'
        ']}]',
        0.05,
    )

    with patch.object(maintenance_ops, "call_deep_reasoning", return_value=response), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Alice Rivera")
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="works_at, trains_at, member_of",
        )

    assert len(results) == 1
    assert results[0][0]["relation"] == "works_at"
    assert results[0][0]["relation"] != "trains_at"
    assert results[0][0]["object"] == "Central Training House"


def test_batch_extract_edges_uses_expanded_output_budget_for_compound_edges():
    facts = [
        {"id": "fact-6a", "text": "Maya lives with her partner David.", "owner_id": "default"},
        {"id": "fact-6b", "text": "David works at North Pier and manages Kai.", "owner_id": "default"},
    ]
    metrics = maintenance_ops.JanitorMetrics()
    captured = {}

    def _fake_call(prompt: str, max_tokens: int, timeout: float):
        captured["max_tokens"] = max_tokens
        return ('[{"fact": 1, "edges": []}, {"fact": 2, "edges": []}]', 0.05)

    with patch.object(maintenance_ops, "call_deep_reasoning", side_effect=_fake_call), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ):
        maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="partner_of, works_at, manages",
        )

    assert captured["max_tokens"] == 1200


def test_batch_extract_edges_retries_smaller_batches_when_parent_batch_returns_empty_response():
    facts = [
        {"id": "fact-7", "text": "Diana has a daughter named Alice", "owner_id": "default"},
        {"id": "fact-8", "text": "David is the user's brother.", "owner_id": "default"},
    ]
    metrics = maintenance_ops.JanitorMetrics()
    responses = [
        ("", 0.05),
        (
            '[{"fact": 1, "edges": ['
            '{"subject":"Diana","subject_type":"Person","relation":"parent_of","object":"Alice","object_type":"Person"}'
            ']}]',
            0.05,
        ),
        (
            '[{"fact": 1, "edges": ['
            '{"subject":"David","subject_type":"Person","relation":"sibling_of","object":"the user","object_type":"Person"}'
            ']}]',
            0.05,
        ),
    ]

    with patch.object(maintenance_ops, "call_deep_reasoning", side_effect=responses), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert [edge["relation"] for edge in results[0]] == ["parent_of"]
    assert [edge["relation"] for edge in results[1]] == ["sibling_of"]
    assert not metrics.has_errors
    assert any("retrying edge extraction in smaller batches" in item["warning"] for item in metrics.warnings)


def test_batch_extract_edges_retries_smaller_batches_when_batch_response_is_not_a_list():
    facts = [
        {"id": "fact-9", "text": "Diana has a daughter named Alice", "owner_id": "default"},
        {"id": "fact-10", "text": "David is the user's brother.", "owner_id": "default"},
    ]
    metrics = maintenance_ops.JanitorMetrics()
    responses = [
        ('{"status":"incomplete"}', 0.05),
        (
            '[{"fact": 1, "edges": ['
            '{"subject":"Diana","subject_type":"Person","relation":"parent_of","object":"Alice","object_type":"Person"}'
            ']}]',
            0.05,
        ),
        (
            '[{"fact": 1, "edges": ['
            '{"subject":"David","subject_type":"Person","relation":"sibling_of","object":"the user","object_type":"Person"}'
            ']}]',
            0.05,
        ),
    ]

    with patch.object(maintenance_ops, "call_deep_reasoning", side_effect=responses), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert [edge["relation"] for edge in results[0]] == ["parent_of"]
    assert [edge["relation"] for edge in results[1]] == ["sibling_of"]
    assert not metrics.has_errors
    assert any("Batch edge response was not a list" in item["warning"] for item in metrics.warnings)


def test_batch_extract_edges_retries_smaller_batches_when_batch_raises_retryable_transport_error():
    facts = [
        {"id": "fact-11", "text": "Diana has a daughter named Alice", "owner_id": "default"},
        {"id": "fact-12", "text": "David is the user's brother.", "owner_id": "default"},
    ]
    metrics = maintenance_ops.JanitorMetrics()
    responses = [
        RuntimeError(
            "LLM call failed after retries while failHard is enabled "
            "(provider=OpenAICodexOAuthLLMProvider, tier=deep, model=gpt-5.4, "
            "error_type=IncompleteRead, error=IncompleteRead(2747 bytes read))."
        ),
        (
            '[{"fact": 1, "edges": ['
            '{"subject":"Diana","subject_type":"Person","relation":"parent_of","object":"Alice","object_type":"Person"}'
            ']}]',
            0.05,
        ),
        (
            '[{"fact": 1, "edges": ['
            '{"subject":"David","subject_type":"Person","relation":"sibling_of","object":"the user","object_type":"Person"}'
            ']}]',
            0.05,
        ),
    ]

    with patch.object(maintenance_ops, "call_deep_reasoning", side_effect=responses), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert [edge["relation"] for edge in results[0]] == ["parent_of"]
    assert [edge["relation"] for edge in results[1]] == ["sibling_of"]
    assert not metrics.has_errors
    assert any("transport failed" in item["warning"] for item in metrics.warnings)


def test_relationship_backfill_filter_is_structural_not_english_keyword_gated():
    assert maintenance_ops._looks_like_relationship_backfill_fact(
        "Conan O'Brien Needs a Friend — funny, light, great for runs."
    ) is True
    assert maintenance_ops._looks_like_relationship_backfill_fact(
        "The assistant suggested Conan O'Brien Needs a Friend, Heavyweight, Smartless, Radiolab, and This American Life as running podcasts.",
        {"source_type": "assistant"},
    ) is True
    assert maintenance_ops._looks_like_relationship_backfill_fact(
        "Sophie est mariee avec Marc."
    ) is True
    assert maintenance_ops._looks_like_relationship_backfill_fact(
        "Implementation option A",
        {"structural_anchor_kind": "assistant_plan_anchor"},
    ) is False


def test_parallel_key_preserves_unicode_task_names():
    assert maintenance_ops._parallel_key("レビュー 処理") == "レビュー_処理"
    assert maintenance_ops._parallel_key("レビュー/処理") == "レビュー_処理"


def test_backfill_edges_sends_broad_candidates_to_llm_extraction():
    assert maintenance_ops._looks_like_relationship_backfill_fact(
        "Sophie está casada con Marc."
    ) is True


def test_backfill_edges_does_not_filter_candidates_by_english_phrases():
    rows = [
        {
            "id": "noise-1",
            "name": "Conan O'Brien Needs a Friend — funny, light, great for runs.",
            "owner_id": "default",
            "attributes": "{}",
        },
        {
            "id": "noise-2",
            "name": "The assistant suggested Conan O'Brien Needs a Friend, Heavyweight, Smartless, Radiolab, and This American Life as running podcasts.",
            "owner_id": "default",
            "attributes": '{"structural_anchor_kind":"assistant_plan_anchor"}',
        },
        {
            "id": "fact-1",
            "name": "Maya lives with her partner David.",
            "owner_id": "default",
            "attributes": "{}",
        },
        {
            "id": "fact-2",
            "name": "Sophie está casada con Marc.",
            "owner_id": "default",
            "attributes": "{}",
        },
    ]

    class _DummyResult:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, sql, params=()):
            if "SELECT n.id" in sql:
                captured["sql"] = sql
                captured["params"] = params
            elif "UPDATE nodes SET attributes" in sql:
                captured.setdefault("updates", []).append((sql, params))
            return _DummyResult(rows)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    metrics = maintenance_ops.JanitorMetrics()
    captured = {}

    def _fake_batch_extract(facts, graph, metrics, relations_list):
        captured["facts"] = facts
        return [[] for _ in facts]

    with patch.object(maintenance_ops, "batch_extract_edges", side_effect=_fake_batch_extract):
        out = maintenance_ops.backfill_edges(
            graph=_Graph(),
            metrics=metrics,
            dry_run=False,
            max_facts=10,
            owner_id="default",
        )

    assert out == {"found": 3, "edges_created": 0, "errors": 0}
    assert "LOWER(n.name) LIKE" not in captured["sql"]
    assert "GLOB" not in captured["sql"]
    assert captured["params"] == (200,)
    assert captured["facts"] == [
        {
            "id": "noise-1",
            "text": "Conan O'Brien Needs a Friend — funny, light, great for runs.",
            "owner_id": "default",
            "attributes": {},
        },
        {
            "id": "fact-1",
            "text": "Maya lives with her partner David.",
            "owner_id": "default",
            "attributes": {},
        },
        {
            "id": "fact-2",
            "text": "Sophie está casada con Marc.",
            "owner_id": "default",
            "attributes": {},
        },
    ]
    assert len(captured["updates"]) == 3


def test_backfill_edges_marks_no_edge_llm_results_as_checked():
    stored_attrs = {}

    class _DummyResult:
        def __init__(self, rows=None):
            self._rows = rows or []

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, sql, params=()):
            if "SELECT n.id" in sql:
                attrs = stored_attrs.get("fact-1", {})
                return _DummyResult(
                    [
                        {
                            "id": "fact-1",
                            "name": "The office plant is named Bartholomew.",
                            "owner_id": "default",
                            "attributes": maintenance_ops.json.dumps(attrs),
                        }
                    ]
                )
            if "UPDATE nodes SET attributes" in sql:
                attrs_json, _updated_at, fact_id = params
                stored_attrs[fact_id] = maintenance_ops.json.loads(attrs_json)
                return _DummyResult()
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    with patch.object(maintenance_ops, "batch_extract_edges", return_value=[[]]) as batch_extract:
        first = maintenance_ops.backfill_edges(
            graph=_Graph(),
            metrics=maintenance_ops.JanitorMetrics(),
            dry_run=False,
            max_facts=10,
            owner_id="default",
        )

    assert first == {"found": 1, "edges_created": 0, "errors": 0}
    assert stored_attrs["fact-1"]["edge_backfill_check_version"] == maintenance_ops.EDGE_BACKFILL_CHECK_VERSION
    assert stored_attrs["fact-1"]["edge_backfill_edges_returned"] == 0
    batch_extract.assert_called_once()

    with patch.object(maintenance_ops, "batch_extract_edges") as second_batch_extract:
        second = maintenance_ops.backfill_edges(
            graph=_Graph(),
            metrics=maintenance_ops.JanitorMetrics(),
            dry_run=True,
            max_facts=10,
            owner_id="default",
        )

    assert second == {"found": 0, "edges_created": 0, "errors": 0}
    second_batch_extract.assert_not_called()


def test_edge_backfill_checked_logs_unreadable_marker(caplog):
    with caplog.at_level("DEBUG", logger=maintenance_ops.__name__):
        assert maintenance_ops._edge_backfill_checked({"edge_backfill_check_version": object()}) is False

    assert "edge_backfill check marker was unreadable" in caplog.text


def test_backfill_edges_missing_edge_id_does_not_mark_fact_checked(caplog):
    stored_attrs = {}

    class _DummyResult:
        def __init__(self, rows=None):
            self._rows = rows or []

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, sql, params=()):
            if "SELECT n.id" in sql:
                return _DummyResult(
                    [
                        {
                            "id": "fact-1",
                            "name": "Maya lives with her partner David.",
                            "owner_id": "default",
                            "attributes": "{}",
                        }
                    ]
                )
            if "UPDATE nodes SET attributes" in sql:
                attrs_json, _updated_at, fact_id = params
                stored_attrs[fact_id] = maintenance_ops.json.loads(attrs_json)
                return _DummyResult()
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    edge = {
        "fact_id": "fact-1",
        "subject": "Maya",
        "relation": "partner_of",
        "object": "David",
    }
    with patch.object(maintenance_ops, "batch_extract_edges", return_value=[[edge]]), \
         patch.object(maintenance_ops, "create_edge", return_value={"status": "created"}), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), \
         caplog.at_level("WARNING", logger=maintenance_ops.__name__):
        out = maintenance_ops.backfill_edges(
            graph=_Graph(),
            metrics=maintenance_ops.JanitorMetrics(),
            dry_run=False,
            max_facts=10,
            owner_id="default",
        )

    assert out == {"found": 1, "edges_created": 0, "errors": 1}
    assert stored_attrs == {}
    assert "returned no edge_id" in caplog.text


def test_backfill_edges_link_failure_still_marks_fact_checked_when_fail_open():
    stored_attrs = {}

    class _DummyResult:
        def __init__(self, rows=None):
            self._rows = rows or []

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, sql, params=()):
            if "SELECT n.id" in sql:
                return _DummyResult(
                    [
                        {
                            "id": "fact-1",
                            "name": "Maya lives with her partner David.",
                            "owner_id": "default",
                            "attributes": "{}",
                        }
                    ]
                )
            if "UPDATE nodes SET attributes" in sql:
                attrs_json, _updated_at, fact_id = params
                stored_attrs[fact_id] = maintenance_ops.json.loads(attrs_json)
                return _DummyResult()
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    edge = {
        "fact_id": "fact-1",
        "subject": "Maya",
        "relation": "partner_of",
        "object": "David",
    }
    with patch.object(maintenance_ops, "batch_extract_edges", return_value=[[edge]]), \
         patch.object(maintenance_ops, "create_edge", return_value={"status": "created", "edge_id": "edge-1"}), \
         patch.object(maintenance_ops, "_link_edge_to_backfilled_fact", side_effect=RuntimeError("link failed")), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        out = maintenance_ops.backfill_edges(
            graph=_Graph(),
            metrics=maintenance_ops.JanitorMetrics(),
            dry_run=False,
            max_facts=10,
            owner_id="default",
        )

    assert out == {"found": 1, "edges_created": 1, "errors": 1}
    assert stored_attrs["fact-1"]["edge_backfill_check_version"] == maintenance_ops.EDGE_BACKFILL_CHECK_VERSION
    assert stored_attrs["fact-1"]["edge_backfill_edges_returned"] == 1


def test_backfill_edges_link_failure_raises_when_failhard():
    class _DummyResult:
        def __init__(self, rows=None):
            self._rows = rows or []

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, sql, params=()):
            if "SELECT n.id" in sql:
                return _DummyResult(
                    [
                        {
                            "id": "fact-1",
                            "name": "Maya lives with her partner David.",
                            "owner_id": "default",
                            "attributes": "{}",
                        }
                    ]
                )
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    edge = {
        "fact_id": "fact-1",
        "subject": "Maya",
        "relation": "partner_of",
        "object": "David",
    }
    with patch.object(maintenance_ops, "batch_extract_edges", return_value=[[edge]]), \
         patch.object(maintenance_ops, "create_edge", return_value={"status": "created", "edge_id": "edge-1"}), \
         patch.object(maintenance_ops, "_link_edge_to_backfilled_fact", side_effect=RuntimeError("link failed")), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="link failed"):
            maintenance_ops.backfill_edges(
                graph=_Graph(),
                metrics=maintenance_ops.JanitorMetrics(),
                dry_run=False,
                max_facts=10,
                owner_id="default",
            )


def test_find_edge_candidates_partial_scan_is_not_english_keyword_gated():
    rows = [
        {"id": "noise-1", "name": "Conan O'Brien Needs a Friend", "type": "Fact", "owner_id": "default"},
        {"id": "fact-1", "name": "Sophie está casada con Marc.", "type": "Fact", "owner_id": "default"},
        {"id": "fact-2", "name": "Sophie est mariee avec Marc.", "type": "Fact", "owner_id": "default"},
    ]

    class _DummyResult:
        def fetchall(self):
            return rows

    class _Conn:
        def execute(self, *_args, **_kwargs):
            return _DummyResult()

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

        def _row_to_node(self, row):
            return SimpleNamespace(**row)

    out = maintenance_ops.find_edge_candidates_optimized(
        graph=_Graph(),
        metrics=maintenance_ops.JanitorMetrics(),
        full_scan=False,
    )

    assert out == [
        {"id": "noise-1", "text": "Conan O'Brien Needs a Friend", "type": "Fact", "owner_id": "default"},
        {"id": "fact-1", "text": "Sophie está casada con Marc.", "type": "Fact", "owner_id": "default"},
        {"id": "fact-2", "text": "Sophie est mariee avec Marc.", "type": "Fact", "owner_id": "default"},
    ]


def test_review_pending_prompt_includes_domain_neutral_role_guardrails():
    captured = {}

    class _DummyResult:
        def __init__(self, rows=None):
            self._rows = rows or []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql)
            if "SELECT COUNT(*) FROM nodes WHERE status = 'pending'" in text:
                return _DummyResult(rows=[(1,)])
            if "SELECT id, type, name, created_at" in text and "FROM nodes" in text:
                return _DummyResult(
                    rows=[
                        {
                            "id": "mem-1",
                            "type": "Fact",
                            "name": "Alice is Solomon Steadman's niece",
                            "created_at": "2026-04-18T00:00:00",
                            "verified": 0,
                            "confidence": 0.8,
                            "source": "unit-test",
                            "session_id": "sess-1",
                            "speaker": "user",
                        }
                    ]
                )
            if "SELECT name, status, source, speaker, attributes FROM nodes WHERE id = ?" in text:
                return _DummyResult(
                    rows=[
                        {
                            "name": "Alice is Solomon Steadman's niece",
                            "status": "pending",
                            "source": "unit-test",
                            "speaker": "user",
                            "attributes": "{}",
                        }
                    ]
                )
            return _DummyResult(rows=[])

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    def _fake_call(user_message=None, system_prompt=None, max_tokens=None, **kwargs):
        captured["system_prompt"] = system_prompt
        return '[{"id":"mem-1","action":"KEEP"}]', 0.05

    fake_cfg = SimpleNamespace(
        models=SimpleNamespace(max_output=lambda tier: 1024),
        core=SimpleNamespace(
            parallel=SimpleNamespace(enabled=False, llm_workers=1, task_workers={})
        ),
    )
    with patch.object(maintenance_ops, "_cfg", fake_cfg), patch.object(
        maintenance_ops, "_owner_display_name", return_value="Solomon"
    ), patch.object(
        maintenance_ops, "_owner_full_name", return_value="Solomon Steadman"
    ), patch.object(
        maintenance_ops, "call_deep_reasoning", side_effect=_fake_call
    ):
        out = maintenance_ops.review_pending_memories(_Graph(), dry_run=True, max_items=1)

    assert out["total_reviewed"] == 1
    prompt = captured.get("system_prompt", "")
    assert "RELATIONSHIP ROLE FIDELITY (MANDATORY)" in prompt
    assert "Do not rewrite one relationship role into a different role family" in prompt
    assert "do NOT emit parent_of" in prompt
    assert "organizational structure terms" in prompt
    assert "preserve direction exactly as stated" in prompt
    assert "Do not infer hidden intermediate hops unless the intermediate relationship is explicitly stated" in prompt


def test_owner_full_name_logs_when_owner_resolution_fails_and_not_fail_hard():
    fake_cfg = SimpleNamespace(
        users=SimpleNamespace(
            default_owner="default",
            identities={"default": SimpleNamespace(person_node_name="Config Owner")},
        )
    )
    with patch.object(maintenance_ops, "_cfg", fake_cfg), patch.object(
        maintenance_ops, "resolve_owner_person", side_effect=RuntimeError("db unavailable")
    ), patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), patch.object(
        maintenance_ops.logger, "warning"
    ) as warn:
        out = maintenance_ops._owner_full_name("default")

    assert out == "Config Owner"
    assert warn.call_count == 1


def test_owner_full_name_raises_when_owner_resolution_fails_and_fail_hard():
    fake_cfg = SimpleNamespace(
        users=SimpleNamespace(
            default_owner="default",
            identities={"default": SimpleNamespace(person_node_name="Config Owner")},
        )
    )
    with patch.object(maintenance_ops, "_cfg", fake_cfg), patch.object(
        maintenance_ops, "resolve_owner_person", side_effect=RuntimeError("db unavailable")
    ), patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        try:
            maintenance_ops._owner_full_name("default")
            assert False, "expected RuntimeError in fail-hard mode"
        except RuntimeError as exc:
            assert "Unable to resolve owner person" in str(exc)


def test_owner_full_name_falls_back_to_top_level_owner_name_for_unresolved_slug():
    fake_cfg = SimpleNamespace(
        owner_name="Casey River",
        users=SimpleNamespace(
            default_owner="casey-river",
            identities={},
        ),
    )
    with patch.object(maintenance_ops, "_cfg", fake_cfg), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=None
    ):
        out = maintenance_ops._owner_full_name("casey-river")

    assert out == "Casey River"


def test_owner_full_name_falls_back_to_configured_slug_display_name_when_unresolved():
    fake_cfg = SimpleNamespace(
        users=SimpleNamespace(
            default_owner="casey-river",
            identities={},
        ),
    )
    with patch.object(maintenance_ops, "_cfg", fake_cfg), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=None
    ):
        out = maintenance_ops._owner_full_name("casey-river")

    assert out == "Casey River"
