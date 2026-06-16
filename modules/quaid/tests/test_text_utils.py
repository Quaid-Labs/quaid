"""Unit tests for lib.tokens text comparison utilities."""

import sys
import os

# Ensure the plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from lib.tokens import texts_are_near_identical, extract_key_tokens, is_subset_overlap_candidate


class TestTextsAreNearIdentical:
    """Tests for texts_are_near_identical()."""

    def test_identical_strings(self):
        assert texts_are_near_identical(
            "Quaid likes coffee",
            "Quaid likes coffee"
        ) is True

    def test_punctuation_only_difference(self):
        assert texts_are_near_identical(
            "Quaid likes coffee.",
            "Quaid likes coffee"
        ) is True

    def test_different_proper_noun(self):
        assert texts_are_near_identical(
            "Quaid's sister is Kuato",
            "Quaid's sister is Hauser"
        ) is False

    def test_subject_object_swap(self):
        assert texts_are_near_identical(
            "Quaid gave Melina a ring",
            "Melina gave Quaid a ring"
        ) is False

    def test_trivial_word_difference(self):
        assert texts_are_near_identical(
            "The cat is big",
            "A cat is big"
        ) is True

    def test_empty_strings(self):
        assert texts_are_near_identical("", "") is True

    def test_completely_different(self):
        assert texts_are_near_identical(
            "Quaid likes coffee",
            "The weather is tropical"
        ) is False

    def test_different_number(self):
        assert texts_are_near_identical(
            "Quaid is 35",
            "Quaid is 36"
        ) is False

    def test_compact_script_spacing_and_width_variants_match(self):
        assert texts_are_near_identical(
            "美玲は青い窯で陶芸作品を焼いている",
            "美玲 は 青い 窯 で 陶芸 作品 を 焼いている",
        ) is True
        assert texts_are_near_identical(
            "ＡＰＰ設定は有効です",
            "app 設定 は 有効 です",
        ) is True

    def test_compact_script_different_facts_do_not_match(self):
        assert texts_are_near_identical(
            "美玲は青い窯で陶芸作品を焼いている",
            "美玲は赤い窯で陶芸作品を焼いている",
        ) is False


class TestExtractKeyTokens:
    """Basic tests for extract_key_tokens()."""

    def test_extracts_meaningful_words(self):
        tokens = extract_key_tokens("Quaid likes coffee in the morning")
        assert "quaid" in tokens
        assert "coffee" in tokens
        assert "morning" in tokens

    def test_filters_only_short_ascii_noise(self):
        tokens = extract_key_tokens("the cat is in the hat")
        assert "the" in tokens
        assert "cat" in tokens
        assert "hat" in tokens
        assert "is" not in tokens
        assert "in" not in tokens

    def test_respects_max_tokens(self):
        tokens = extract_key_tokens("one two three four five six seven eight nine ten", max_tokens=3)
        assert len(tokens) <= 3

    def test_empty_string(self):
        tokens = extract_key_tokens("")
        assert tokens == []

    def test_short_ascii_tokens_only(self):
        tokens = extract_key_tokens("is a an")
        assert tokens == []

    def test_extracts_unicode_tokens(self):
        tokens = extract_key_tokens("美玲 和 云门")
        assert "美玲" in tokens
        assert "云门" in tokens


class TestSubsetOverlapCandidate:
    """Tests for is_subset_overlap_candidate()."""

    def test_detects_subset_overlap_without_negation(self):
        assert is_subset_overlap_candidate(
            "Solomon has a dog named Baxter who loves tennis balls",
            "Solomon has a dog named Baxter",
        ) is True

    def test_negation_word_boundary_avoids_substring_false_hit(self):
        # "knowledge"/"notorious" should not count as negation tokens.
        assert is_subset_overlap_candidate(
            "Solomon shares knowledge about Baxter and tennis balls",
            "Solomon shares knowledge about Baxter",
        ) is True
        assert is_subset_overlap_candidate(
            "Solomon has a notorious dog named Baxter who loves tennis balls",
            "Solomon has a notorious dog named Baxter",
        ) is True

    def test_negation_words_do_not_language_gate_subset_overlap(self):
        assert is_subset_overlap_candidate(
            "Solomon does not keep blue notes near desk",
            "Solomon keep blue notes near desk",
        ) is True
