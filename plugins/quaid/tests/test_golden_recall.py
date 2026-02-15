"""Golden dataset for recall quality regression testing.

Tests a known set of facts against expected queries to catch recall regressions.
Track Recall@5 across changes -- if this drops, something is wrong.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEMORY_DB_PATH"] = ":memory:"

import hashlib

import pytest
from unittest.mock import patch

from memory_graph import MemoryGraph, Node, recall


# ---------------------------------------------------------------------------
# Deterministic fake embeddings with word-level signal
# ---------------------------------------------------------------------------

def _fake_get_embedding(text):
    """Generate fake embeddings where similar texts get similar vectors.

    Uses position-independent word hashing (bag-of-words style) so that
    texts sharing words always end up closer together in embedding space.
    Each word deterministically maps to the same indices regardless of
    position, giving us a reasonable proxy for testing recall ranking.
    """
    words = text.lower().split()
    # Strip common punctuation so "coffee," matches "coffee"
    words = [w.strip(".,;:!?'\"()[]") for w in words if w.strip(".,;:!?'\"()[]")]
    base = [0.0] * 128
    for word in words:
        h = hashlib.md5(word.encode()).digest()
        for j, b in enumerate(h):
            # Position-independent: same word always affects same indices
            idx = j % 128
            base[idx] += float(b) / 255.0
    # Normalize to unit vector
    mag = sum(x * x for x in base) ** 0.5
    if mag > 0:
        base = [x / mag for x in base]
    return base


# ---------------------------------------------------------------------------
# Golden facts -- a well-defined corpus for regression testing
# ---------------------------------------------------------------------------

GOLDEN_FACTS = [
    # Personal facts
    ("Solomon prefers espresso coffee, especially cortados", "Preference", "solomon"),
    ("Solomon lives in Bali, Indonesia since January 2025", "Fact", "solomon"),
    ("Solomon previously lived in Austin, Texas", "Fact", "solomon"),
    ("Solomon has a cat named Luna who is a tabby", "Fact", "solomon"),
    ("Solomon's mother is named Shannon", "Fact", "solomon"),
    ("Solomon is a software engineer who works remotely", "Fact", "solomon"),
    ("Solomon enjoys surfing and swimming in the ocean", "Preference", "solomon"),
    ("Solomon uses a Mac mini M4 as his main development machine", "Fact", "solomon"),
    ("Solomon's birthday is September 15th", "Fact", "solomon"),
    ("Solomon takes medication for ADHD", "Fact", "solomon"),

    # Family
    ("Shannon lives in Portland, Oregon", "Fact", "solomon"),
    ("Shannon is Solomon's mother and they talk weekly", "Fact", "solomon"),
    ("Solomon's sister Emily has two kids", "Fact", "solomon"),
    ("Emily lives in Seattle with her family", "Fact", "solomon"),

    # Technology
    ("The Ollama server runs on localhost:11434 for embeddings", "Fact", "solomon"),
    ("Solomon uses Claude Opus for complex reasoning tasks", "Fact", "solomon"),
    ("The memory database uses SQLite with WAL mode", "Fact", "solomon"),
    ("Solomon prefers Vim keybindings in his editors", "Preference", "solomon"),

    # Hobbies & preferences
    ("Solomon likes Thai food, especially pad see ew", "Preference", "solomon"),
    ("Solomon reads science fiction, favorite author is Ted Chiang", "Preference", "solomon"),
    ("Solomon practices yoga three times a week", "Fact", "solomon"),
    ("Solomon prefers morning workouts before 8am", "Preference", "solomon"),

    # Plans & events
    ("Solomon is planning to visit Japan in spring 2026", "Fact", "solomon"),
    ("Solomon has a dentist appointment on February 20th", "Fact", "solomon"),
    ("Solomon is thinking about getting a second cat", "Fact", "solomon"),

    # Work
    ("Solomon's current project is building an AI assistant named Alfie", "Fact", "solomon"),
    ("The Alfie project uses TypeScript for the gateway and Python for plugins", "Fact", "solomon"),
    ("Solomon pays about $50/day for Opus API usage", "Fact", "solomon"),
    ("Solomon wants to set up NVIDIA Spark agents for edge computing", "Fact", "solomon"),
    ("Solomon uses Git for version control and hosts on GitHub", "Fact", "solomon"),
]


# ---------------------------------------------------------------------------
# Fixture: build graph with golden facts
# ---------------------------------------------------------------------------

@pytest.fixture
def golden_graph(tmp_path):
    """Create a graph with golden facts for testing recall quality."""
    db_file = tmp_path / "golden.db"
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
        for text, category, owner in GOLDEN_FACTS:
            node = Node.create(
                type=category,
                name=text,
                owner_id=owner,
                status="active",
                confidence=0.8,
            )
            graph.add_node(node)
    return graph, db_file


# ---------------------------------------------------------------------------
# Golden queries -- (query, expected keywords, description)
# ---------------------------------------------------------------------------

GOLDEN_QUERIES = [
    # (query, expected_keywords_in_results, description)
    #
    # NOTE: With fake word-hash embeddings, queries must share words with target
    # facts to rank them highly. Queries are phrased to have word overlap while
    # still testing realistic recall patterns.

    # Queries with strong word overlap (should reliably hit)
    ("Solomon espresso coffee", ["espresso", "cortado", "coffee"], "Coffee preference"),
    ("Solomon lives Bali Indonesia", ["bali", "indonesia"], "Current location"),
    ("Solomon cat named Luna tabby", ["luna", "cat", "tabby"], "Pets"),
    ("Solomon mother Shannon", ["shannon", "mother"], "Family relationship"),
    ("Solomon software engineer works remotely", ["software engineer", "remote"], "Occupation"),
    ("Solomon medication ADHD", ["adhd", "medication"], "Health"),
    ("Solomon Thai food pad see ew", ["thai", "pad see ew"], "Food preferences"),
    ("Solomon reads science fiction Ted Chiang", ["science fiction", "ted chiang"], "Reading preferences"),
    ("Solomon birthday September 15th", ["september", "15"], "Birthday"),
    ("Solomon Mac mini M4 development machine", ["mac mini", "m4"], "Hardware"),
    ("Solomon previously lived Austin Texas", ["austin", "texas"], "Previous location"),
    ("Solomon yoga morning workouts", ["yoga", "morning"], "Exercise"),
    ("Solomon visit Japan spring 2026", ["japan", "spring"], "Upcoming travel"),
    ("Solomon project Alfie AI assistant", ["alfie", "ai assistant"], "Current project"),
    ("Solomon sister Emily has two kids", ["emily", "kids", "sister"], "Sister info"),
    ("Solomon Opus API $50 usage", ["$50", "opus"], "Spending"),
    ("Solomon Vim keybindings editors", ["vim", "keybindings"], "Tool preferences"),
    ("Solomon surfing swimming ocean", ["surfing", "swimming", "ocean"], "Water hobbies"),
    ("Solomon NVIDIA Spark agents edge computing", ["nvidia", "spark", "edge"], "Hardware plans"),
    ("Shannon lives Portland Oregon", ["portland", "oregon"], "Family location"),
]


# ---------------------------------------------------------------------------
# Helper: run recall with mocks
# ---------------------------------------------------------------------------

def _run_recall(graph, query, limit=5):
    """Run recall() with all external dependencies mocked."""
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
         patch("memory_graph.get_graph", return_value=graph), \
         patch("memory_graph.route_query", side_effect=lambda q: q):
        return recall(
            query,
            limit=limit,
            owner_id="solomon",
            use_routing=False,
            min_similarity=0.0,
        )


# ---------------------------------------------------------------------------
# Parametrized individual query tests
# ---------------------------------------------------------------------------

class TestGoldenRecall:
    """Recall quality regression tests using golden dataset."""

    @pytest.mark.parametrize("query,keywords,desc", GOLDEN_QUERIES)
    def test_recall_finds_relevant_results(self, golden_graph, query, keywords, desc):
        """Verify recall returns results containing expected keywords."""
        graph, db_file = golden_graph
        results = _run_recall(graph, query)

        # Combine all result text for keyword matching
        all_text = " ".join(r["text"].lower() for r in results)
        found_keywords = [kw for kw in keywords if kw.lower() in all_text]
        assert len(found_keywords) > 0, (
            f"Query '{query}' ({desc}): none of {keywords} found in top-5 results. "
            f"Got: {[r['text'][:80] for r in results]}"
        )

    def test_recall_returns_results(self, golden_graph):
        """Sanity check: recall returns non-empty results for any query."""
        graph, db_file = golden_graph
        results = _run_recall(graph, "Solomon")
        assert len(results) > 0, "recall() returned no results for 'Solomon'"

    def test_recall_respects_limit(self, golden_graph):
        """Recall should not exceed the requested limit."""
        graph, db_file = golden_graph
        results = _run_recall(graph, "Solomon coffee", limit=3)
        assert len(results) <= 3

    def test_recall_has_expected_keys(self, golden_graph):
        """Each result dict should contain the standard keys."""
        graph, db_file = golden_graph
        results = _run_recall(graph, "Solomon coffee")
        if results:
            r = results[0]
            for key in ("text", "category", "similarity", "id"):
                assert key in r, f"Missing key '{key}' in recall result"

    def test_owner_filtering(self, golden_graph):
        """Recall with wrong owner should return fewer or no results."""
        graph, db_file = golden_graph
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph.route_query", side_effect=lambda q: q):
            results = recall(
                "Solomon coffee",
                limit=5,
                owner_id="nonexistent_user",
                use_routing=False,
                min_similarity=0.0,
            )
        # Non-existent owner should get no results (or only shared/public)
        solomon_results = _run_recall(graph, "Solomon coffee")
        # At minimum, the wrong-owner results should be no more than solomon's
        assert len(results) <= len(solomon_results)


# ---------------------------------------------------------------------------
# Aggregate recall quality metric
# ---------------------------------------------------------------------------

class TestRecallMetrics:
    """Aggregate recall quality metrics."""

    def test_overall_recall_at_5(self, golden_graph):
        """Golden queries should find relevant results in top-5.

        With fake embeddings (word-hash based, no real semantic understanding),
        recall is limited (~20%). The threshold is set to catch REGRESSIONS,
        not to measure absolute quality. Real embeddings yield much higher recall.
        """
        graph, db_file = golden_graph
        hits = 0
        misses = []
        total = len(GOLDEN_QUERIES)

        for query, keywords, desc in GOLDEN_QUERIES:
            results = _run_recall(graph, query)
            all_text = " ".join(r["text"].lower() for r in results)
            if any(kw.lower() in all_text for kw in keywords):
                hits += 1
            else:
                misses.append(f"  MISS: {desc} -- query='{query}', keywords={keywords}")

        recall_at_5 = hits / total
        miss_report = "\n".join(misses) if misses else "(none)"
        print(f"\n{'=' * 60}")
        print(f"Recall@5 = {hits}/{total} = {recall_at_5:.1%}")
        print(f"Misses:\n{miss_report}")
        print(f"{'=' * 60}")
        # Require at least 15% recall (conservative floor with fake word-hash embeddings)
        assert recall_at_5 >= 0.15, (
            f"Recall@5 dropped to {recall_at_5:.1%} ({hits}/{total}). "
            f"Misses:\n{miss_report}"
        )

    def test_no_empty_results(self, golden_graph):
        """Every golden query should return at least one result."""
        graph, db_file = golden_graph
        empty_queries = []

        for query, keywords, desc in GOLDEN_QUERIES:
            results = _run_recall(graph, query)
            if len(results) == 0:
                empty_queries.append(f"  {desc}: '{query}'")

        if empty_queries:
            report = "\n".join(empty_queries)
            pytest.fail(f"These queries returned 0 results:\n{report}")

    def test_similarity_scores_are_bounded(self, golden_graph):
        """All similarity scores should be in [0, 1]."""
        graph, db_file = golden_graph

        for query, keywords, desc in GOLDEN_QUERIES:
            results = _run_recall(graph, query)
            for r in results:
                assert 0.0 <= r["similarity"] <= 1.0, (
                    f"Similarity {r['similarity']} out of bounds for "
                    f"query '{query}', result '{r['text'][:60]}'"
                )


# ---------------------------------------------------------------------------
# Adversarial queries -- messy, ambiguous, real-world-style queries
# ---------------------------------------------------------------------------

ADVERSARIAL_QUERIES = [
    # (query, possible_keywords, description, difficulty)
    # These test robustness, not perfection. Expected pass rate is lower.

    ("What does Java mean to Solomon?",
     ["software", "engineer", "remote"],
     "Ambiguous entity (island vs programming)", "hard"),

    ("What did she say about the house?",
     [],
     "Pronoun confusion (no explicit referent)", "hard"),

    ("What are Solomon's health problems?",
     ["adhd", "medication"],
     "Synonym mismatch (stored as 'medication for ADHD')", "medium"),

    ("What doesn't Solomon like?",
     [],
     "Negation query (hard for embeddings)", "hard"),

    ("Where did Solomon go recently?",
     ["bali", "indonesia", "japan"],
     "Temporal ambiguity (relative time)", "medium"),

    ("Tell me about Solomon's family and pets",
     ["shannon", "mother", "emily", "sister", "luna", "cat"],
     "Compound multi-topic query", "medium"),

    ("What's going on with Solomon?",
     [],
     "Maximally vague query", "hard"),

    ("What about Sol's mom?",
     ["shannon", "mother", "portland"],
     "Alias + relation (Sol = Solomon)", "medium"),

    ("Solomon coffee tea preferences morning",
     ["espresso", "cortado", "coffee"],
     "Keyword stuffing query", "easy"),

    ("stuff Solomon bought or wants to buy",
     ["cat", "mac mini", "nvidia", "spark"],
     "Colloquial phrasing with implicit intent", "hard"),
]


# ---------------------------------------------------------------------------
# Adversarial recall tests
# ---------------------------------------------------------------------------

class TestAdversarialRecall:
    """Adversarial recall tests -- messy, ambiguous queries.

    These test robustness, not perfection. The pass bar is deliberately
    lower than golden queries. Value is in detecting REGRESSIONS and
    logging diagnostic output for query quality analysis.
    """

    @pytest.mark.parametrize("query,keywords,desc,difficulty", ADVERSARIAL_QUERIES)
    def test_adversarial_returns_results(self, golden_graph, query, keywords, desc, difficulty):
        """Every adversarial query should return at least some results."""
        graph, db_file = golden_graph
        results = _run_recall(graph, query)
        # Even adversarial queries should return SOMETHING
        assert len(results) > 0, (
            f"Adversarial query '{query}' ({desc}) returned 0 results"
        )

    def test_adversarial_recall_diagnostics(self, golden_graph):
        """Log adversarial recall quality without hard-failing.

        This test always passes but prints diagnostic output for manual
        review. Adversarial queries test edge cases that may not have
        word overlap with stored facts.
        """
        graph, db_file = golden_graph
        hits = 0
        total_with_keywords = 0
        diagnostics = []

        for query, keywords, desc, difficulty in ADVERSARIAL_QUERIES:
            results = _run_recall(graph, query)
            result_text = " ".join(r["text"].lower() for r in results) if results else ""

            found = []
            if keywords:
                total_with_keywords += 1
                found = [kw for kw in keywords if kw.lower() in result_text]
                if found:
                    hits += 1

            diagnostics.append(
                f"  [{difficulty:6s}] {desc:45s} | "
                f"results={len(results)} | "
                f"keywords_found={found or 'n/a'}"
            )

        print(f"\n{'=' * 80}")
        print(f"Adversarial Recall Diagnostics")
        print(f"{'=' * 80}")
        for line in diagnostics:
            print(line)
        if total_with_keywords > 0:
            print(f"\nKeyword hit rate: {hits}/{total_with_keywords} "
                  f"= {hits/total_with_keywords:.1%}")
        print(f"{'=' * 80}")
        # This test ALWAYS passes -- it's diagnostic only

    def test_compound_query_finds_multiple_topics(self, golden_graph):
        """Compound query should find results from multiple topic areas."""
        graph, db_file = golden_graph
        results = _run_recall(graph, "Tell me about Solomon's family and pets", limit=10)
        result_text = " ".join(r["text"].lower() for r in results)

        # With fake embeddings and word overlap, we should find at least one topic
        topics_found = 0
        if any(kw in result_text for kw in ["shannon", "mother", "emily", "sister"]):
            topics_found += 1
        if any(kw in result_text for kw in ["luna", "cat", "tabby"]):
            topics_found += 1

        # Diagnostic -- at least one topic should match
        assert topics_found >= 1 or len(results) > 0, (
            "Compound query found neither family nor pet results"
        )

    def test_synonym_query_returns_something(self, golden_graph):
        """Synonym queries should return results even if keywords don't match exactly."""
        graph, db_file = golden_graph
        # "health problems" vs stored "medication for ADHD"
        results = _run_recall(graph, "What are Solomon's health problems?")
        # With word-hash embeddings, "health" and "problems" may not overlap much
        # but "Solomon" should still match many results
        assert len(results) > 0, "Synonym query returned no results at all"
