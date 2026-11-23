"""Fixed lists of allowed values.

They are stored in the database as plain text (VARCHAR), not as MySQL ENUM,
so the schema stays portable between database engines.
"""
import enum


class BatchStatus(str, enum.Enum):
    draft = "draft"            # files uploaded, HR still reviewing matches
    ready = "ready"            # HR ticked "I have checked the matches": waiting for the (later) Send step
    sending = "sending"        # Send was clicked, sending in progress
    completed = "completed"    # every payslip has been processed


class ProcessingState(str, enum.Enum):
    processing = "processing"        # matching / PDF text check running in the background
    done = "done"
    failed = "failed"                # stopped by an error or a server restart: "Re-run matching" fixes it


class MatchStatus(str, enum.Enum):
    pending = "pending"              # valid PDF stored, matching has not run for it yet
    invalid = "invalid"              # not a usable PDF (reason in invalid_reason); never stored on disk
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
    no_text = "no_text"                # scanned image - check skipped (information only)
    name_not_found = "name_not_found"  # the PDF has text but no employee's name in it (information only)
    possible_mismatch = "possible_mismatch"  # another employee's name is in the PDF instead


class SendState(str, enum.Enum):
    idle = "idle"          # not started, or everything finished
    running = "running"    # the worker is sending queued payslips
    stopped = "stopped"    # HR pressed Stop: queued rows went back to pending
    halted = "halted"      # sending stopped by itself (e.g. WhatsApp rejected the access token)


class PayslipStatus(str, enum.Enum):
    pending = "pending"    # ready, not queued yet (or put back by Stop)
    queued = "queued"
    sending = "sending"    # saved BEFORE the network call; a restart turns this into "uncertain"
    uncertain = "uncertain"  # may or may not have been sent: never retried automatically
    sent = "sent"
    delivered = "delivered"
    read = "read"
    failed = "failed"
