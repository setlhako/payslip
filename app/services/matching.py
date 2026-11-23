"""Matching payslip file names to employees (the FINAL design; see CLAUDE.md "Payslip matching").

1. A file name becomes a SET of words: extension dropped, then lowercase, accents stripped, split on _ - . and spaces
   (anything that is not a letter), digits dropped. `thabo_molefe.pdf` and `Molefe-Thabo (2).pdf` -> {thabo, molefe}.
2. Each ACTIVE employee has the set of words of first_name + surname.
3. Result:
   - exact:        the sets are equal for exactly ONE employee           -> auto-matched
   - needs_review: anything less certain: typo, partial name, middle names, or the same name shared by 2+ employees
                   -> top 3 suggestions, each with a similarity score (0-100)
   - unmatched:    no employee scores at least SUGGEST_MIN

Similarity: words are paired one-to-one by closeness (difflib ratio >= WORD_MIN counts as the same word, so a typo
still pairs). score = 100 * 2 * (sum of pair ratios) / (words in file + words in employee name).
So a middle name costs a little (80), a typo a little (96), a missing surname more (67), sharing only a surname 50.

Speed: word-to-word closeness is computed once per distinct file word (cached) against the distinct words of all
employees, and only employees owning a close word are scored, so 300 files x 300 employees takes well under a second.
Pure functions: no database, no file access, nothing is logged.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import PurePath

from app.services.names import name_words

SUGGEST_MIN = 50       # below this score nobody is suggested (the file is "unmatched")
WORD_MIN = 0.8         # two words this similar count as the same word (typo tolerance)
MAX_SUGGESTIONS = 3
EXACT, NEEDS_REVIEW, UNMATCHED = "exact", "needs_review", "unmatched"


def file_words(filename: str) -> frozenset[str]:
    """The word set of a file name. The extension is removed first so 'pdf' is never a word."""
    stem = PurePath((filename or "").replace("\\", "/").rsplit("/", 1)[-1]).stem
    return frozenset(name_words(stem))


def employee_words(first_name: str, surname: str) -> frozenset[str]:
    return frozenset(name_words(first_name)) | frozenset(name_words(surname))


@dataclass
class MatchResult:
    status: str                                   # exact / needs_review / unmatched
    employee_id: int | None = None                # set only for exact
    score: int | None = None                      # 100 for exact; the best suggestion's score otherwise
    suggestions: list[tuple[int, int]] = field(default_factory=list)   # [(employee_id, score)], best first, max 3
    shared_name: bool = False                     # needs_review because 2+ employees have exactly this name


def _ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if abs(len(a) - len(b)) > 3 or (a[0] != b[0] and a[-1] != b[-1]):   # cheap filters: typos rarely change both ends
        return 0.0
    m = SequenceMatcher(None, a, b)
    if m.real_quick_ratio() < WORD_MIN or m.quick_ratio() < WORD_MIN:
        return 0.0
    r = m.ratio()
    return r if r >= WORD_MIN else 0.0


class MatchIndex:
    """Built once per batch from the active employees (anything with id, first_name, surname)."""

    def __init__(self, employees) -> None:
        self.words_of: dict[int, frozenset[str]] = {}
        self.exact: dict[frozenset[str], list[int]] = defaultdict(list)
        self.by_word: dict[str, list[int]] = defaultdict(list)
        for e in employees:
            words = employee_words(e.first_name, e.surname)
            if not words:
                continue
            self.words_of[e.id] = words
            self.exact[words].append(e.id)
            for w in words:
                self.by_word[w].append(e.id)
        self._vocab = list(self.by_word)
        self._close: dict[str, list[tuple[str, float]]] = {}

    def _close_words(self, word: str) -> list[tuple[str, float]]:
        """Employee words that are the same as, or a typo of, this file word, with their closeness."""
        hit = self._close.get(word)
        if hit is None:
            hit = [(v, r) for v in self._vocab if (r := _ratio(word, v)) > 0]
            self._close[word] = hit
        return hit

    def score(self, fwords: frozenset[str], employee_id: int) -> int:
        ewords = self.words_of[employee_id]
        pairs = sorted(((r, fw, ew) for fw in fwords for ew, r in self._close_words(fw) if ew in ewords), reverse=True)
        used_f: set[str] = set()
        used_e: set[str] = set()
        total = 0.0
        for r, fw, ew in pairs:                       # best pairs first, each word used once
            if fw not in used_f and ew not in used_e:
                used_f.add(fw)
                used_e.add(ew)
                total += r
        return round(100 * 2 * total / (len(fwords) + len(ewords)))

    def match(self, filename: str) -> MatchResult:
        fwords = file_words(filename)
        if not fwords:
            return MatchResult(UNMATCHED)
        same = self.exact.get(fwords, [])
        if len(same) == 1:
            return MatchResult(EXACT, employee_id=same[0], score=100, suggestions=[(same[0], 100)])

        candidates: set[int] = set()
        for fw in fwords:
            for ew, _ in self._close_words(fw):
                candidates.update(self.by_word[ew])
        scored = sorted(((self.score(fwords, i), i) for i in candidates), key=lambda t: (-t[0], t[1]))
        top = [(i, s) for s, i in scored if s >= SUGGEST_MIN][:MAX_SUGGESTIONS]
        if len(same) > 1:                              # the same name belongs to several people: HR chooses
            top = [(i, 100) for i in sorted(same)][:MAX_SUGGESTIONS]
            return MatchResult(NEEDS_REVIEW, score=100, suggestions=top, shared_name=True)
        if not top:
            return MatchResult(UNMATCHED)
        return MatchResult(NEEDS_REVIEW, score=top[0][1], suggestions=top)
