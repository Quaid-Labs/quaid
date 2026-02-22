"""Tests for edge normalization: inverse maps, synonym maps, symmetric ordering."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from janitor import (
    _normalize_edge, _INVERSE_MAP, _SYNONYM_MAP, _SYMMETRIC_RELATIONS,
    _SEED_RELATIONS,
)


class TestInverseFlipping:
    """Relations in _INVERSE_MAP flip subject/object."""

    def test_child_of_flips_to_parent_of(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "child_of", "Lori", "Person"
        )
        assert r == "parent_of"
        assert s == "Lori"
        assert o == "Quaid"

    def test_son_of_flips_to_parent_of(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "son_of", "Lori", "Person"
        )
        assert r == "parent_of"
        assert s == "Lori"
        assert o == "Quaid"

    def test_daughter_of_flips_to_parent_of(self):
        s, st, r, o, ot = _normalize_edge(
            "Hauser", "Person", "daughter_of", "Lori", "Person"
        )
        assert r == "parent_of"
        assert s == "Lori"
        assert o == "Hauser"

    def test_owned_by_flips_to_owns(self):
        s, st, r, o, ot = _normalize_edge(
            "Villa Atmata", "Place", "owned_by", "Quaid", "Person"
        )
        assert r == "owns"
        assert s == "Quaid"
        assert st == "Person"
        assert o == "Villa Atmata"
        assert ot == "Place"

    def test_pet_of_flips_to_has_pet(self):
        s, st, r, o, ot = _normalize_edge(
            "Richter", "Concept", "pet_of", "Quaid", "Person"
        )
        assert r == "has_pet"
        assert s == "Quaid"
        assert o == "Richter"

    def test_employs_flips_to_works_at(self):
        s, st, r, o, ot = _normalize_edge(
            "Facebook", "Organization", "employs", "Quaid", "Person"
        )
        assert r == "works_at"
        assert s == "Quaid"
        assert o == "Facebook"

    def test_managed_by_flips_to_manages(self):
        s, st, r, o, ot = _normalize_edge(
            "Project", "Concept", "managed_by", "Quaid", "Person"
        )
        assert r == "manages"
        assert s == "Quaid"
        assert o == "Project"


class TestSynonymResolution:
    """Relations in _SYNONYM_MAP rename without flipping."""

    def test_mother_of_becomes_parent_of_same_direction(self):
        # mother_of(Lori, Quaid) → parent_of(Lori, Quaid) — NO flip
        s, st, r, o, ot = _normalize_edge(
            "Lori", "Person", "mother_of", "Quaid", "Person"
        )
        assert r == "parent_of"
        assert s == "Lori"
        assert o == "Quaid"

    def test_father_of_becomes_parent_of_same_direction(self):
        s, st, r, o, ot = _normalize_edge(
            "Kent", "Person", "father_of", "Quaid", "Person"
        )
        assert r == "parent_of"
        assert s == "Kent"
        assert o == "Quaid"

    def test_married_to_becomes_spouse_of(self):
        s, st, r, o, ot = _normalize_edge(
            "Alice", "Person", "married_to", "Zed", "Person"
        )
        assert r == "spouse_of"
        assert s == "Alice"
        assert o == "Zed"

    def test_resides_in_becomes_lives_in(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "resides_in", "Bali", "Place"
        )
        assert r == "lives_in"

    def test_likes_becomes_prefers(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "likes", "Espresso", "Concept"
        )
        assert r == "prefers"

    def test_enjoys_becomes_prefers(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "enjoys", "Board Games", "Concept"
        )
        assert r == "prefers"

    def test_engaged_to_becomes_partner_of(self):
        s, st, r, o, ot = _normalize_edge(
            "Alice", "Person", "engaged_to", "Zed", "Person"
        )
        assert r == "partner_of"
        assert s == "Alice"
        assert o == "Zed"

    def test_suffers_from_becomes_diagnosed_with(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "suffers_from", "Colitis", "Concept"
        )
        assert r == "diagnosed_with"

    def test_works_for_becomes_works_at(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "works_for", "Facebook", "Organization"
        )
        assert r == "works_at"
        assert s == "Quaid"
        assert o == "Facebook"


class TestSymmetricOrdering:
    """Symmetric relations order entities alphabetically."""

    def test_spouse_of_already_ordered(self):
        s, st, r, o, ot = _normalize_edge(
            "Alice", "Person", "spouse_of", "Zed", "Person"
        )
        assert s == "Alice"
        assert o == "Zed"

    def test_spouse_of_needs_flip(self):
        s, st, r, o, ot = _normalize_edge(
            "Zed", "Person", "spouse_of", "Alice", "Person"
        )
        assert s == "Alice"
        assert o == "Zed"

    def test_friend_of_alphabetical(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "friend_of", "De Kai", "Person"
        )
        assert s == "De Kai"
        assert o == "Quaid"

    def test_sibling_of_alphabetical(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "sibling_of", "Kuato", "Person"
        )
        assert s == "Kuato"
        assert o == "Quaid"

    def test_knows_alphabetical(self):
        s, st, r, o, ot = _normalize_edge(
            "Lori", "Person", "knows", "Kuato", "Person"
        )
        assert s == "Kuato"
        assert o == "Lori"

    def test_colleague_of_alphabetical(self):
        s, st, r, o, ot = _normalize_edge(
            "Zach", "Person", "colleague_of", "Alice", "Person"
        )
        assert s == "Alice"
        assert o == "Zach"


class TestNonSymmetricPreservesOrder:
    """Non-symmetric relations keep the original subject/object order."""

    def test_lives_in(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "lives_in", "Bali", "Place"
        )
        assert s == "Quaid"
        assert o == "Bali"

    def test_works_at(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "works_at", "Facebook", "Organization"
        )
        assert s == "Quaid"
        assert o == "Facebook"

    def test_parent_of_not_symmetric(self):
        """parent_of is directional: parent is always subject."""
        s, st, r, o, ot = _normalize_edge(
            "Lori", "Person", "parent_of", "Quaid", "Person"
        )
        assert s == "Lori"
        assert o == "Quaid"

    def test_prefers_not_symmetric(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "prefers", "Espresso", "Concept"
        )
        assert s == "Quaid"
        assert o == "Espresso"


class TestPassthrough:
    """Unknown relations pass through unchanged."""

    def test_custom_relation(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "custom_relation", "Something", "Concept"
        )
        assert r == "custom_relation"
        assert s == "Quaid"
        assert o == "Something"


class TestCombinedTransforms:
    """Test combinations of inverse + synonym + symmetric."""

    def test_inverse_to_non_symmetric(self):
        """child_of flips to parent_of, which is NOT symmetric."""
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "child_of", "Lori", "Person"
        )
        assert r == "parent_of"
        assert s == "Lori"
        assert o == "Quaid"

    def test_synonym_to_symmetric(self):
        """married_to → spouse_of (synonym), then alphabetical ordering (symmetric)."""
        s, st, r, o, ot = _normalize_edge(
            "Zed", "Person", "married_to", "Alice", "Person"
        )
        assert r == "spouse_of"
        assert s == "Alice"  # alphabetical
        assert o == "Zed"

    def test_synonym_to_symmetric_already_ordered(self):
        s, st, r, o, ot = _normalize_edge(
            "Alice", "Person", "engaged_to", "Zed", "Person"
        )
        assert r == "partner_of"
        assert s == "Alice"
        assert o == "Zed"


class TestWhitespaceAndCase:
    """Edge cases with formatting."""

    def test_whitespace_in_relation(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "  Lives In  ", "Bali", "Place"
        )
        assert r == "lives_in"

    def test_spaces_become_underscores(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "works at", "Facebook", "Organization"
        )
        assert r == "works_at"

    def test_uppercase_normalized(self):
        s, st, r, o, ot = _normalize_edge(
            "Quaid", "Person", "LIVES_IN", "Bali", "Place"
        )
        assert r == "lives_in"


class TestSeedRelationsIntegrity:
    """Verify seed relations don't contain inverses or synonyms."""

    def test_no_inverses_in_seed(self):
        for rel in _SEED_RELATIONS:
            assert rel not in _INVERSE_MAP, \
                f"'{rel}' in both _SEED_RELATIONS and _INVERSE_MAP"

    def test_no_synonyms_in_seed(self):
        for rel in _SEED_RELATIONS:
            assert rel not in _SYNONYM_MAP, \
                f"'{rel}' in both _SEED_RELATIONS and _SYNONYM_MAP"

    def test_all_inverse_targets_are_canonical(self):
        for inv, canonical in _INVERSE_MAP.items():
            assert canonical in _SEED_RELATIONS, \
                f"_INVERSE_MAP['{inv}'] = '{canonical}' not in _SEED_RELATIONS"

    def test_all_synonym_targets_are_canonical(self):
        for syn, canonical in _SYNONYM_MAP.items():
            assert canonical in _SEED_RELATIONS, \
                f"_SYNONYM_MAP['{syn}'] = '{canonical}' not in _SEED_RELATIONS"

    def test_symmetric_relations_are_in_seed(self):
        for rel in _SYMMETRIC_RELATIONS:
            assert rel in _SEED_RELATIONS, \
                f"'{rel}' in _SYMMETRIC_RELATIONS but not _SEED_RELATIONS"

    def test_no_overlap_inverse_synonym(self):
        """A relation shouldn't be in both maps."""
        overlap = set(_INVERSE_MAP.keys()) & set(_SYNONYM_MAP.keys())
        assert not overlap, f"Relations in both maps: {overlap}"


class TestCausalEdges:
    """Causal edge type normalization."""

    def test_led_to_inverse(self):
        """led_to(A, B) flips to caused_by(B, A)."""
        s, st, r, o, ot = _normalize_edge(
            "stress", "Fact", "led_to", "symptoms", "Fact"
        )
        assert r == "caused_by"
        assert s == "symptoms"
        assert o == "stress"

    def test_caused_inverse(self):
        """caused(A, B) flips to caused_by(B, A)."""
        s, st, r, o, ot = _normalize_edge(
            "negotiations", "Fact", "caused", "anxiety", "Fact"
        )
        assert r == "caused_by"
        assert s == "anxiety"
        assert o == "negotiations"

    def test_resulted_in_inverse(self):
        """resulted_in(A, B) flips to caused_by(B, A)."""
        s, st, r, o, ot = _normalize_edge(
            "bad diet", "Fact", "resulted_in", "weight gain", "Fact"
        )
        assert r == "caused_by"
        assert s == "weight gain"
        assert o == "bad diet"

    def test_triggered_inverse(self):
        """triggered(A, B) flips to caused_by(B, A)."""
        s, st, r, o, ot = _normalize_edge(
            "medication", "Fact", "triggered", "side effects", "Fact"
        )
        assert r == "caused_by"
        assert s == "side effects"
        assert o == "medication"

    def test_because_of_synonym(self):
        """because_of(A, B) stays caused_by(A, B) — same direction."""
        s, st, r, o, ot = _normalize_edge(
            "sleep issues", "Fact", "because_of", "stress", "Fact"
        )
        assert r == "caused_by"
        assert s == "sleep issues"
        assert o == "stress"

    def test_due_to_synonym(self):
        """due_to(A, B) stays caused_by(A, B) — same direction."""
        s, st, r, o, ot = _normalize_edge(
            "fatigue", "Fact", "due_to", "insomnia", "Fact"
        )
        assert r == "caused_by"
        assert s == "fatigue"
        assert o == "insomnia"

    def test_caused_by_canonical_untouched(self):
        """caused_by is already canonical — no transformation needed."""
        s, st, r, o, ot = _normalize_edge(
            "symptom", "Fact", "caused_by", "condition", "Fact"
        )
        assert r == "caused_by"
        assert s == "symptom"
        assert o == "condition"
