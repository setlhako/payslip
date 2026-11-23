"""File-name matching: normalisation, exact / needs-review / unmatched, suggestions and scores, speed."""
import random
import string
import time
from types import SimpleNamespace as E

import pytest

from app.services.matching import EXACT, NEEDS_REVIEW, UNMATCHED, MatchIndex, file_words


def index(*people):
    return MatchIndex([E(id=i, first_name=f, surname=s) for i, (f, s) in enumerate(people, 1)])


# ---- normalising file names ------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,words", [
    ("thabo_molefe.pdf", {"thabo", "molefe"}),
    ("Molefe-Thabo.PDF", {"thabo", "molefe"}),
    ("THABO MOLEFE.pdf", {"thabo", "molefe"}),
    ("thabo.molefe.pdf", {"thabo", "molefe"}),
    ("Thabo_Molefe_2026_11.pdf", {"thabo", "molefe"}),              # digits dropped
    ("payslip 12 thabo molefe (2).pdf", {"payslip", "thabo", "molefe"}),
    ("Zoë_O'Brien.pdf", {"zoe", "obrien"}),                          # accents stripped, apostrophe dropped
    ("folder/sub\\thabo_molefe.pdf", {"thabo", "molefe"}),           # folders never count as words
    ("12345.pdf", set()),
    ("thabo_molefe", {"thabo", "molefe"}),                           # no extension
])
def test_file_name_becomes_a_set_of_words(name, words):
    assert file_words(name) == frozenset(words)


# ---- the three outcomes ----------------------------------------------------------------------------------------

def test_exact_match_in_either_order_case_and_separator():
    ix = index(("Thabo", "Molefe"), ("Neo", "Dube"))
    for name in ("thabo_molefe.pdf", "molefe_thabo.pdf", "THABO-MOLEFE.pdf", "Molefe Thabo.pdf", "thabo_molefe_3.pdf"):
        r = ix.match(name)
        assert (r.status, r.employee_id, r.score) == (EXACT, 1, 100), name


def test_exact_ignores_accents_on_either_side():
    ix = index(("Zoë", "O'Brien"))
    assert ix.match("zoe_obrien.pdf").status == EXACT


def test_typo_needs_review_with_a_high_score_suggestion():
    ix = index(("Thabo", "Molefe"), ("Neo", "Dube"))
    r = ix.match("thabo_molefee.pdf")
    assert r.status == NEEDS_REVIEW and r.employee_id is None
    assert r.suggestions[0][0] == 1 and r.suggestions[0][1] >= 90


def test_missing_letter_typo_is_still_suggested():
    r = index(("Thabo", "Molefe")).match("tabo_molefe.pdf")
    assert r.status == NEEDS_REVIEW and r.suggestions[0][0] == 1


def test_middle_name_needs_review():
    ix = index(("Thabo", "Molefe"), ("Neo", "Dube"))
    r = ix.match("thabo_james_molefe.pdf")
    assert r.status == NEEDS_REVIEW and [i for i, _ in r.suggestions] == [1] and 70 <= r.suggestions[0][1] < 100


def test_partial_name_needs_review():
    r = index(("Thabo", "Molefe"), ("Neo", "Dube")).match("molefe.pdf")
    assert r.status == NEEDS_REVIEW and r.suggestions[0][0] == 1


def test_same_name_shared_by_two_employees_needs_review_and_lists_both():
    ix = index(("Ana", "Silva"), ("Ana", "Silva"), ("Neo", "Dube"))
    r = ix.match("ana_silva.pdf")
    assert r.status == NEEDS_REVIEW and r.employee_id is None and r.shared_name
    assert sorted(i for i, _ in r.suggestions) == [1, 2] and all(s == 100 for _, s in r.suggestions)
    assert ix.match("neo_dube.pdf").status == EXACT                        # a unique name next to them is unaffected


def test_unknown_person_and_no_letters_are_unmatched():
    ix = index(("Thabo", "Molefe"), ("Neo", "Dube"))
    assert ix.match("quentin_zzyzx.pdf").status == UNMATCHED
    assert ix.match("12345.pdf").status == UNMATCHED
    assert ix.match("").status == UNMATCHED


def test_sharing_only_a_surname_is_below_the_suggestion_threshold_when_the_file_has_more_words():
    r = index(("Thabo", "Molefe")).match("zed_quentin_molefe.pdf")
    assert r.status == UNMATCHED


def test_at_most_three_suggestions_best_first():
    ix = index(("Anna", "Smith"), ("Anne", "Smith"), ("Anni", "Smith"), ("Anny", "Smith"), ("Zed", "Other"))
    r = ix.match("ann_smith.pdf")                      # "ann" is a near miss for all four first names
    scores = [s for _, s in r.suggestions]
    assert r.status == NEEDS_REVIEW and len(r.suggestions) == 3 and scores == sorted(scores, reverse=True)


def test_only_the_employees_given_to_the_index_can_match():
    """The caller passes ACTIVE employees only, so an inactive person can never be auto-matched."""
    ix = index(("Neo", "Dube"))
    assert ix.match("thabo_molefe.pdf").status == UNMATCHED


def test_scores_are_between_0_and_100_and_exact_is_100():
    ix = index(("Thabo", "Molefe"))
    assert ix.match("thabo_molefe.pdf").score == 100
    for name in ("thabo.pdf", "molefe_thabo_x_y_z.pdf", "thabo_molefee.pdf"):
        r = ix.match(name)
        assert all(0 <= s <= 100 for _, s in r.suggestions)


def test_matching_does_not_depend_on_employee_order():
    people = [("Thabo", "Molefe"), ("Neo", "Dube"), ("Ana", "Silva")]
    a = index(*people).match("silva_ana.pdf")
    b = index(*reversed(people)).match("silva_ana.pdf")
    assert a.status == b.status == EXACT


# ---- speed -----------------------------------------------------------------------------------------------------

def test_300_files_against_300_employees_is_fast():
    rng = random.Random(7)
    word = lambda: "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(4, 9)))  # noqa: E731
    people = [(word(), word()) for _ in range(300)]
    ix = index(*people)
    names = []
    for first, last in people:
        names.append(f"{first}_{last}.pdf")                                         # exact
    for _ in range(300):
        names.append(f"{word()}_{word()}.pdf")                                      # strangers (worst case: scan everyone)
    started = time.perf_counter()
    results = [ix.match(n) for n in names]
    elapsed = time.perf_counter() - started
    assert sum(r.status == EXACT for r in results[:300]) == 300
    assert elapsed < 3.0, f"matching 600 files took {elapsed:.2f}s"
