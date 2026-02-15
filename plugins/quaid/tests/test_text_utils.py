"""Unit tests for lib.tokens text comparison utilities."""

import sys
import os

# Ensure the plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from lib.tokens import texts_are_near_identical, extract_key_tokens


class TestTextsAreNearIdentical:
    """Tests for texts_are_near_identical()."""

    def test_identical_strings(self):
        assert texts_are_near_identical(
            "Solomon likes coffee",
            "Solomon likes coffee"
        ) is True

    def test_punctuation_only_difference(self):
        assert texts_are_near_identical(
            "Solomon likes coffee.",
            "Solomon likes coffee"
        ) is True

    def test_different_proper_noun(self):
        assert texts_are_near_identical(
            "Solomon's sister is Amber",
            "Solomon's sister is Shannon"
        ) is False

    def test_subject_object_swap(self):
        assert texts_are_near_identical(
            "Solomon gave Yuni a ring",
            "Yuni gave Solomon a ring"
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
            "Solomon likes coffee",
            "The weather is tropical"
        ) is False

    def test_different_number(self):
        assert texts_are_near_identical(
            "Solomon is 35",
            "Solomon is 36"
        ) is False


class TestExtractKeyTokens:
    """Basic tests for extract_key_tokens()."""

    def test_extracts_meaningful_words(self):
        tokens = extract_key_tokens("Solomon likes coffee in the morning")
        assert "solomon" in tokens
        assert "coffee" in tokens
        assert "morning" in tokens

    def test_filters_stopwords(self):
        tokens = extract_key_tokens("the cat is in the hat")
        assert "the" not in tokens
        assert "cat" in tokens
        assert "hat" in tokens

    def test_respects_max_tokens(self):
        tokens = extract_key_tokens("one two three four five six seven eight nine ten", max_tokens=3)
        assert len(tokens) <= 3

    def test_empty_string(self):
        tokens = extract_key_tokens("")
        assert tokens == []

    def test_all_stopwords(self):
        tokens = extract_key_tokens("the is a an")
        assert tokens == []
