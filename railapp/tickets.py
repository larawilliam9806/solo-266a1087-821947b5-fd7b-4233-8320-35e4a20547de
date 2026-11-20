"""Ticket-id index and duplicate detection.

The same ticket must not enter a day's settlement twice.  To catch that
without rescanning every day's original, the application keeps a small index
that mirrors the ``ticket_id`` column of each day's *current* official
original:

* on startup every day whose indexed revision differs from ``days.current_rev``
  is (re)built from that day's original (the upgrade path for legacy data);
* a new publication replaces the day's index rows inside the very same
  ``BEGIN IMMEDIATE`` transaction that inserts the publication, so the new
  revision and its index become visible together (or neither does).

The investigation page and the confirmation-time duplicate check both read
the index only; a confirmation never opens another day's original.
"""

from collections import Counter

from . import convert, db
from .errors import AppError

TICKET_COLUMN = "ticket_id"

STATE_READY = "ready"
STATE_BLANK = "blank_tickets"
STATE_MISSING = "missing_ticket_id"
STATE_UNAVAILABLE = "unavailable"

_PREVIEW_LIST_LIMIT = 200


class TicketScan:
    """Result of scanning one parsed original for ticket ids."""

    __slots__ = ("has_ticket_id", "row_count", "counts", "occurrences", "blanks")

    def __init__(self, has_ticket_id, row_count):
        self.has_ticket_id = has_ticket_id
        self.row_count = row_count
        self.counts = Counter()
        # (ticket_id, physical line number) for every non-blank row.
        self.occurrences = []
        # (data row number, physical line number) for every row with a blank id.
        self.blanks = []

    def add(self, ticket_id, row_no, line_no):
        ticket_id = ticket_id.strip()
        if not ticket_id:
            self.blanks.append((row_no, line_no))
            return
        self.counts[ticket_id] += 1
        self.occurrences.append((ticket_id, line_no))


def scan_parsed(parsed):
    """Index ticket ids from an already parsed CSV.

    ``ticket_id`` values are matched after stripping surrounding whitespace;
    a value that is empty after stripping counts as a blank ticket id.  The
    physical line number refers to the original upload (header is line 1).
    """
    scan = TicketScan(TICKET_COLUMN in parsed.header, len(parsed.rows))
    if not scan.has_ticket_id:
        return scan
    position = parsed.header.index(TICKET_COLUMN)
    for row_no, (line_no, fields) in enumerate(parsed.rows, start=1):
        scan.add(fields[position], row_no, line_no)
    return scan


def state_for(scan):
    if not scan.has_ticket_id:
        return STATE_MISSING
    if scan.blanks:
        return STATE_BLANK
    return STATE_READY


def replace_day_index(conn, *, day, rev, original_sha256, scan, indexed_at,
                      state=None, detail=None):
    """Replace the day's index with ``scan`` (inside the caller's transaction)."""
    db.replace_day_ticket_index(
        conn,
        day=day,
        rev=rev,
        original_sha256=original_sha256,
        has_ticket_id=1 if scan.has_ticket_id else 0,
        ticket_count=sum(scan.counts.values()),
        blank_count=len(scan.blanks),
        state=state or state_for(scan),
        detail=detail,
        indexed_at=indexed_at,
        occurrences=scan.occurrences,
        blanks=scan.blanks,
    )


def find_conflicts(conn, day, scan):
    """Return tickets whose occurrence count rises and stays above one in total.

    ``old`` counts and the global totals both come from the index.  A ticket
    is only checked when its count in this day increases, which lets existing
    duplicates be reduced day by day and lets other fields be corrected
    without changing the occurrence count.
    """
    old = db.day_ticket_counts(conn, day)
    candidates = {
        ticket: count
        for ticket, count in scan.counts.items()
        if count > old.get(ticket, 0)
    }
    if not candidates:
        return []
    global_counts = db.global_ticket_counts(conn, candidates)
    conflicts = []
    for ticket, new_count in candidates.items():
        old_count = old.get(ticket, 0)
        # The current index still holds this day's old occurrences; swap them
        # for the new count to obtain the post-publication total.
        total_after = global_counts.get(ticket, 0) - old_count + new_count
        if total_after > 1:
            conflicts.append(
                {
                    "ticket_id": ticket,
                    "new_count": new_count,
                    "old_count": old_count,
                    "total_after": total_after,
                }
            )
    conflicts.sort(key=lambda item: item["ticket_id"])
    return conflicts


def preview_snapshot(scan, conflicts):
    """Small, JSON-safe summary stored with a preview and shown to the user."""
    duplicates = [
        {"ticket_id": ticket, "count": count}
        for ticket, count in scan.counts.items()
        if count > 1
    ]
    duplicates.sort(key=lambda item: (-item["count"], item["ticket_id"]))
    ok = scan.has_ticket_id and not scan.blanks and not conflicts
    return {
        "has_ticket_id": scan.has_ticket_id,
        "row_count": scan.row_count,
        "blank_row_count": len(scan.blanks),
        "blank_rows": [
            {"row": row_no, "line": line_no}
            for row_no, line_no in scan.blanks[:_PREVIEW_LIST_LIMIT]
        ],
        "in_day_duplicate_count": len(duplicates),
        "in_day_duplicates": duplicates[:_PREVIEW_LIST_LIMIT],
        "cross_day_conflict_count": len(conflicts),
        "cross_day_conflicts": conflicts[:_PREVIEW_LIST_LIMIT],
        "status": "ok" if ok else "incompatible",
    }


def backfill(conn, store):
    """Build/refresh the index for every day's current official revision.

    Runs at startup, so upgrading an existing installation indexes all legacy
    originals once.  A day that cannot be read is recorded as unavailable with
    a short detail; the next startup retries it.  Returns a per-day report.
    """
    report = []
    rows = conn.execute(
        "SELECT day, current_rev FROM days ORDER BY day"
    ).fetchall()
    for row in rows:
        day = row["day"]
        rev = int(row["current_rev"])
        existing = db.get_day_ticket_index(conn, day)
        if (
            existing is not None
            and int(existing["rev"]) == rev
            and existing["state"] != STATE_UNAVAILABLE
        ):
            continue
        publication = db.get_publication(conn, day, rev)
        if publication is None:
            continue
        digest = publication["original_sha256"]
        now = db.iso(db.utcnow())
        try:
            raw = store.read_bytes(digest)
            parsed = convert.parse_csv_bytes(raw)
            scan = scan_parsed(parsed)
        except (convert.CsvProblem, AppError, OSError) as exc:
            detail = str(getattr(exc, "message", exc))[:500]
            with db.transaction(conn):
                db.replace_day_ticket_index(
                    conn,
                    day=day,
                    rev=rev,
                    original_sha256=digest,
                    has_ticket_id=0,
                    ticket_count=0,
                    blank_count=0,
                    state=STATE_UNAVAILABLE,
                    detail=detail,
                    indexed_at=now,
                )
            report.append(
                {"day": day, "rev": rev, "state": STATE_UNAVAILABLE, "detail": detail}
            )
            continue
        with db.transaction(conn):
            replace_day_index(
                conn,
                day=day,
                rev=rev,
                original_sha256=digest,
                scan=scan,
                indexed_at=now,
            )
        report.append({"day": day, "rev": rev, "state": state_for(scan), "detail": None})
    return report
