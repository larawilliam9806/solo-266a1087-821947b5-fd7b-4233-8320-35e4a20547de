"""SQLite access helpers and schema."""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import domain

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    downstream    TEXT PRIMARY KEY,
    columns_json  TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    version       INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS days (
    day          TEXT PRIMARY KEY,
    current_rev  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    day                    TEXT NOT NULL,
    rev                    INTEGER NOT NULL,
    original_name          TEXT NOT NULL,
    original_size          INTEGER NOT NULL,
    original_sha256        TEXT NOT NULL,
    settlement_sha256      TEXT NOT NULL,
    settlement_size        INTEGER NOT NULL,
    ridership_sha256       TEXT NOT NULL,
    ridership_size         INTEGER NOT NULL,
    settlement_fingerprint TEXT NOT NULL,
    ridership_fingerprint  TEXT NOT NULL,
    settlement_version     INTEGER NOT NULL,
    ridership_version      INTEGER NOT NULL,
    settlement_columns     TEXT NOT NULL,
    ridership_columns      TEXT NOT NULL,
    row_count              INTEGER NOT NULL,
    created_at             TEXT NOT NULL,
    PRIMARY KEY (day, rev)
);

CREATE TABLE IF NOT EXISTS previews (
    id                     TEXT PRIMARY KEY,
    day                    TEXT NOT NULL,
    base_rev               INTEGER NOT NULL,
    settlement_version     INTEGER NOT NULL,
    ridership_version      INTEGER NOT NULL,
    original_name          TEXT NOT NULL,
    original_size          INTEGER NOT NULL,
    original_sha256        TEXT NOT NULL,
    result_json            TEXT NOT NULL,
    convertible            INTEGER NOT NULL,
    created_at             TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    consumed_at            TEXT
);

-- One row per day, describing the ticket_id index of that day's *current*
-- official original.  It is replaced in the same transaction as a new
-- publication, so the index and the revision always become visible together.
CREATE TABLE IF NOT EXISTS day_ticket_index (
    day             TEXT PRIMARY KEY,
    rev             INTEGER NOT NULL,
    original_sha256 TEXT NOT NULL,
    has_ticket_id   INTEGER NOT NULL,
    ticket_count    INTEGER NOT NULL,
    blank_count     INTEGER NOT NULL,
    state           TEXT NOT NULL,
    detail          TEXT,
    indexed_at      TEXT NOT NULL
);

-- Every non-blank ticket occurrence in a day's current original.  ``line_no``
-- is the physical line number in the original CSV (header is line 1).
CREATE TABLE IF NOT EXISTS ticket_occurrences (
    day       TEXT NOT NULL,
    rev       INTEGER NOT NULL,
    ticket_id TEXT NOT NULL,
    line_no   INTEGER NOT NULL
);

-- Physical line numbers of rows whose ticket_id is blank (legacy originals).
CREATE TABLE IF NOT EXISTS ticket_blanks (
    day     TEXT NOT NULL,
    rev     INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    PRIMARY KEY (day, rev, line_no)
);

CREATE INDEX IF NOT EXISTS idx_publications_day ON publications(day, rev);
CREATE INDEX IF NOT EXISTS idx_previews_expiry ON previews(expires_at);
CREATE INDEX IF NOT EXISTS idx_ticket_occurrences_ticket
    ON ticket_occurrences(ticket_id, day);
CREATE INDEX IF NOT EXISTS idx_ticket_occurrences_day
    ON ticket_occurrences(day);
"""

PREVIEW_TTL_SECONDS = 2 * 60 * 60


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn, *, immediate=False):
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn):
    conn.executescript(SCHEMA)
    now = iso(utcnow())
    for downstream in domain.DOWNSTREAM_ORDER:
        columns = domain.DEFAULT_CONTRACTS[downstream]
        conn.execute(
            "INSERT OR IGNORE INTO contracts VALUES (?, ?, ?, ?, ?)",
            (
                downstream,
                json.dumps(columns, ensure_ascii=False),
                domain.fingerprint(columns),
                1,
                now,
            ),
        )


def get_contract(conn, downstream):
    row = conn.execute(
        """SELECT downstream, columns_json, fingerprint, version, updated_at
           FROM contracts WHERE downstream = ?""",
        (downstream,),
    ).fetchone()
    if row is None:
        return None
    return {
        "downstream": row["downstream"],
        "columns": json.loads(row["columns_json"]),
        "fingerprint": row["fingerprint"],
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


def get_contracts(conn):
    return {name: get_contract(conn, name) for name in domain.DOWNSTREAM_ORDER}


def update_contract(conn, downstream, columns):
    """Replace a contract and bump its version.  Never touches publications.

    A no-op edit (identical canonical columns) does not bump the version, so
    it cannot invalidate previews that are still pending confirmation.
    """
    fp = domain.fingerprint(columns)
    current = get_contract(conn, downstream)
    if current is not None and current["fingerprint"] == fp:
        return current
    conn.execute(
        """UPDATE contracts
           SET columns_json = ?, fingerprint = ?, version = version + 1, updated_at = ?
           WHERE downstream = ?""",
        (json.dumps(columns, ensure_ascii=False), fp, iso(utcnow()), downstream),
    )
    return get_contract(conn, downstream)


def day_revision(conn, day):
    row = conn.execute("SELECT current_rev FROM days WHERE day = ?", (day,)).fetchone()
    return int(row["current_rev"]) if row else 0


def get_publication(conn, day, rev):
    row = conn.execute(
        "SELECT * FROM publications WHERE day = ? AND rev = ?", (day, rev)
    ).fetchone()
    return dict(row) if row else None


def list_publications(conn, day, limit=200):
    rows = conn.execute(
        "SELECT * FROM publications WHERE day = ? ORDER BY rev DESC LIMIT ?", (day, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def list_days(conn, limit=200):
    rows = conn.execute(
        """SELECT d.day, d.current_rev, d.updated_at,
                  (SELECT count(*) FROM publications p WHERE p.day = d.day) AS revision_count
           FROM days d ORDER BY d.day DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_day_ticket_index(conn, day):
    row = conn.execute(
        "SELECT * FROM day_ticket_index WHERE day = ?", (day,)
    ).fetchone()
    return dict(row) if row else None


def list_day_ticket_index(conn):
    rows = conn.execute(
        "SELECT * FROM day_ticket_index ORDER BY day"
    ).fetchall()
    return [dict(r) for r in rows]


def replace_day_ticket_index(conn, *, day, rev, original_sha256, has_ticket_id,
                             ticket_count, blank_count, state, detail,
                             indexed_at, occurrences=None, blanks=None):
    """Atomically replace one day's ticket index.

    Caller supplies the enclosing transaction: the index must flip together
    with the publication it describes.
    """
    conn.execute("DELETE FROM ticket_occurrences WHERE day = ?", (day,))
    conn.execute("DELETE FROM ticket_blanks WHERE day = ?", (day,))
    conn.execute(
        """INSERT INTO day_ticket_index
           (day, rev, original_sha256, has_ticket_id, ticket_count, blank_count,
            state, detail, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(day) DO UPDATE SET
             rev = excluded.rev,
             original_sha256 = excluded.original_sha256,
             has_ticket_id = excluded.has_ticket_id,
             ticket_count = excluded.ticket_count,
             blank_count = excluded.blank_count,
             state = excluded.state,
             detail = excluded.detail,
             indexed_at = excluded.indexed_at""",
        (day, rev, original_sha256, has_ticket_id, ticket_count, blank_count,
         state, detail, indexed_at),
    )
    if occurrences:
        conn.executemany(
            "INSERT INTO ticket_occurrences (day, rev, ticket_id, line_no) VALUES (?, ?, ?, ?)",
            [(day, rev, ticket, line) for ticket, line in occurrences],
        )
    if blanks:
        conn.executemany(
            "INSERT INTO ticket_blanks (day, rev, line_no) VALUES (?, ?, ?)",
            [(day, rev, line) for _row, line in blanks],
        )


def day_ticket_counts(conn, day):
    """Occurrence counts of the day's current index, keyed by ticket id."""
    rows = conn.execute(
        "SELECT ticket_id, count(*) AS n FROM ticket_occurrences "
        "WHERE day = ? GROUP BY ticket_id",
        (day,),
    ).fetchall()
    return {r["ticket_id"]: int(r["n"]) for r in rows}


def global_ticket_counts(conn, tickets):
    """Totals across all days' current indexes for the given ticket ids.

    Queried in chunks so a large batch never exceeds SQLite's bound-variable
    limit.
    """
    tickets = list(tickets)
    totals = {}
    chunk_size = 400
    for start in range(0, len(tickets), chunk_size):
        chunk = tickets[start:start + chunk_size]
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""SELECT ticket_id, count(*) AS n FROM ticket_occurrences
                WHERE ticket_id IN ({marks}) GROUP BY ticket_id""",
            chunk,
        ).fetchall()
        totals.update({r["ticket_id"]: int(r["n"]) for r in rows})
    return totals


def occurrences_for_ticket(conn, ticket_id):
    rows = conn.execute(
        """SELECT day, rev, line_no FROM ticket_occurrences
           WHERE ticket_id = ? ORDER BY day, line_no""",
        (ticket_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_day_occurrences(conn, day):
    rows = conn.execute(
        """SELECT ticket_id, line_no FROM ticket_occurrences
           WHERE day = ? ORDER BY line_no""",
        (day,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_day_blanks(conn, day):
    rows = conn.execute(
        "SELECT line_no FROM ticket_blanks WHERE day = ? ORDER BY line_no", (day,)
    ).fetchall()
    return [int(r["line_no"]) for r in rows]


def duplicate_tickets(conn):
    """Tickets occurring more than once across all days' current indexes."""
    rows = conn.execute(
        """SELECT ticket_id, count(*) AS total, count(DISTINCT day) AS day_count
           FROM ticket_occurrences
           GROUP BY ticket_id
           HAVING count(*) > 1
           ORDER BY total DESC, ticket_id"""
    ).fetchall()
    return [dict(r) for r in rows]


def create_preview(conn, *, preview_id, day, base_rev, contract_versions,
                   original_name, original_size, original_sha256, result,
                   convertible):
    now = utcnow()
    expires = now + timedelta(seconds=PREVIEW_TTL_SECONDS)
    conn.execute(
        """INSERT INTO previews (id, day, base_rev, settlement_version,
                                 ridership_version, original_name, original_size,
                                 original_sha256, result_json, convertible,
                                 created_at, expires_at, consumed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            preview_id,
            day,
            base_rev,
            contract_versions["settlement"],
            contract_versions["ridership"],
            original_name,
            original_size,
            original_sha256,
            json.dumps(result, ensure_ascii=False),
            1 if convertible else 0,
            iso(now),
            iso(expires),
        ),
    )
    return iso(expires)


def get_preview(conn, preview_id):
    row = conn.execute("SELECT * FROM previews WHERE id = ?", (preview_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["result"] = json.loads(item.pop("result_json"))
    return item


def delete_preview(conn, preview_id):
    conn.execute("DELETE FROM previews WHERE id = ?", (preview_id,))


def purge_expired_previews(conn, now=None):
    now = now or utcnow()
    rows = conn.execute(
        "SELECT id FROM previews WHERE expires_at < ?", (iso(now),)
    ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        conn.executemany("DELETE FROM previews WHERE id = ?", [(i,) for i in ids])
    return ids


def wait_for_write_lock(conn, attempts=200, delay=0.05):
    """Best-effort helper used by tests to serialise around a busy database."""
    for _ in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return
        except sqlite3.OperationalError:
            time.sleep(delay)
    raise sqlite3.OperationalError("database is locked")
