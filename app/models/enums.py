"""Fixed lists of allowed values.

They are stored in the database as plain text (VARCHAR), not as MySQL ENUM,
so the schema stays portable between database engines.
"""
import enum


class BatchStatus(str, enum.Enum):
    draft = "draft"            # files uploaded, HR still reviewing matches
    sending = "sending"        # Send was clicked, sending in progress
    completed = "completed"    # every payslip has been processed


class MatchStatus(str, enum.Enum):
    exact = "exact"                  # name word-set equals exactly one employee
    needs_review = "needs_review"    # fuzzy / partial / shared name - HR must confirm
    unmatched = "unmatched"          # nothing found


class MatchMethod(str, enum.Enum):
    filename_exact = "filename_exact"
    filename_fuzzy = "filename_fuzzy"
    manual = "manual"                # HR picked the employee by hand
    none = "none"


class PdfCheck(str, enum.Enum):
    not_checked = "not_checked"
    ok = "ok"                          # matched employee's name found in the PDF text
    no_text = "no_text"                # scanned image - check skipped
    possible_mismatch = "possible_mismatch"  # another employee's name is in the PDF instead


class PayslipStatus(str, enum.Enum):
    pending = "pending"
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    read = "read"
    failed = "failed"
