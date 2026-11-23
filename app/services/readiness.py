"""What HR sees for each file, and when a batch may be marked "Ready to send". Pure rules over data already loaded.

display_status(): the badge: exact, confirmed (by HR), needs_review, possible_mismatch, unmatched, invalid, pending.
problems():       plain-language reasons a file is NOT ready (empty list = ready). Computed live from the current
                  employee data, so editing an employee's phone fixes the batch without any re-run.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from app.models.enums import BatchStatus, MatchMethod, MatchStatus, PdfCheck, ProcessingState

STATUS_ORDER = ("exact", "confirmed", "needs_review", "possible_mismatch", "unmatched", "invalid")
STATUS_LABELS = {
    "exact": "Exact", "confirmed": "Confirmed by HR", "needs_review": "Needs review",
    "possible_mismatch": "Possible mismatch", "unmatched": "Unmatched", "invalid": "Invalid file", "pending": "Checking",
}


def display_status(p) -> str:
    if p.match_status == MatchStatus.invalid.value:
        return "invalid"
    if p.match_status == MatchStatus.pending.value:
        return "pending"
    if p.employee_id is None:
        return MatchStatus.needs_review.value if p.match_status == MatchStatus.needs_review.value else "unmatched"
    if not p.confirmed:
        return "possible_mismatch" if p.pdf_check == PdfCheck.possible_mismatch.value else "needs_review"
    return "confirmed" if p.match_method == MatchMethod.manual.value else "exact"


@dataclass
class Problem:
    code: str
    text: str


@dataclass
class BatchContext:
    """Facts about the whole batch that single rows need to know about."""
    employees: dict                                   # id -> Employee (every employee that any row points at)
    files_by_employee: dict[int, list[str]] = field(default_factory=dict)   # employee id -> file names (valid rows)
    duplicate_names: set[str] = field(default_factory=set)                  # lower-cased names used more than once


def build_context(rows, employees_by_id: dict) -> BatchContext:
    files: dict[int, list[str]] = defaultdict(list)
    names: dict[str, int] = defaultdict(int)
    for p in rows:
        names[p.original_filename.lower()] += 1
        if p.employee_id and p.match_status != MatchStatus.invalid.value:
            files[p.employee_id].append(p.original_filename)
    return BatchContext(employees_by_id, dict(files), {n for n, c in names.items() if c > 1})


def problems(p, ctx: BatchContext) -> list[Problem]:
    status = display_status(p)
    if status == "invalid":
        return [Problem("invalid", p.invalid_reason or "This file cannot be used.")]
    if status == "pending":
        return [Problem("pending", "Still being checked.")]
    out: list[Problem] = []
    employee = ctx.employees.get(p.employee_id) if p.employee_id else None
    if employee is None:
        out.append(Problem("unmatched", "Not matched to an employee yet."))
    elif status in ("needs_review", "possible_mismatch"):
        if status == "possible_mismatch":
            other = ctx.employees.get(p.text_names_employee_id)
            who = f" ({other.full_name})" if other else ""
            out.append(Problem("mismatch", f"The name inside the PDF looks like a different employee{who}. Check the match."))
        else:
            out.append(Problem("unconfirmed", "The match has not been confirmed yet."))
    if employee is not None:
        if not employee.active:
            out.append(Problem("inactive", "This employee is inactive."))
        if not employee.phone:
            out.append(Problem("no_phone", "This employee has no phone number."))
        if not employee.id_last4:
            out.append(Problem("no_id", "This employee has no ID number, so the PDF password cannot be set."))
        if len(ctx.files_by_employee.get(employee.id, [])) > 1:
            out.append(Problem("two_files", "Another file in this batch is matched to the same employee."))
    if p.original_filename.lower() in ctx.duplicate_names:
        out.append(Problem("duplicate_name", "The same file name was uploaded more than once. Remove the extra copy."))
    return out


def summary(rows) -> dict[str, int]:
    counts = {k: 0 for k in (*STATUS_ORDER, "pending")}
    for p in rows:
        counts[display_status(p)] += 1
    counts["total"] = len(rows)
    return counts


def ready_blockers(batch, rows, ctx: BatchContext) -> list[str]:
    """Plain reasons the batch cannot be marked ready yet (empty list = it can)."""
    if batch.status != BatchStatus.draft.value:
        return ["This batch is already marked ready."] if batch.status == BatchStatus.ready.value else ["This batch can no longer be changed."]
    if batch.files_deleted_at:
        return ["The files of this batch were deleted after the retention period."]
    if batch.processing_state == ProcessingState.processing.value:
        return ["Matching is still running. Wait for it to finish."]
    if batch.processing_state == ProcessingState.failed.value:
        return ["Matching did not finish. Press Re-run matching."]
    if not rows:
        return ["The batch has no files yet."]
    blockers: list[str] = []
    bad = [(p, problems(p, ctx)) for p in rows]
    bad = [(p, pr) for p, pr in bad if pr]
    if bad:
        invalid = sum(1 for p, _ in bad if display_status(p) == "invalid")
        blockers.append(f"{len(bad)} file{'s' if len(bad) != 1 else ''} still need attention"
                        + (f" ({invalid} invalid: remove or replace them)" if invalid else "") + ".")
    return blockers
