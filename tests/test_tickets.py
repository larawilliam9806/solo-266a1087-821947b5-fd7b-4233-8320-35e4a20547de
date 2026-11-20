"""Ticket-id index, duplicate investigation and publish gate."""

import sqlite3

from conftest import DEFAULT_CSV, preview_and_confirm, upload

HEADER = (
    "ticket_id,passenger_id,train_no,amount,travel_date,"
    "depart_station,arrive_station,passenger_count\n"
)


def make_csv(rows, header=HEADER):
    return (header + "".join(rows)).encode("utf-8")


def row(ticket, passenger="P1", amount="55", travel="2026-11-01", count="1"):
    return f"{ticket},{passenger},G1,{amount},{travel},北京南,上海虹桥,{count}\n"


def test_confirm_publishes_and_index_matches_current_revision(client):
    preview, confirmed = preview_and_confirm(client)
    assert confirmed.status_code == 201
    report = client.get("/api/tickets/duplicates").get_json()
    assert report["duplicate_count"] == 0
    days = {d["day"]: d for d in report["days"]}
    assert days["2026-11-01"]["rev"] == 1
    assert days["2026-11-01"]["state"] == "ready"
    assert days["2026-11-01"]["ticket_count"] == 3


def test_missing_ticket_column_blocks_publish(client):
    data = (
        "passenger_id,train_no,amount,travel_date\n"
        "P1,G1,10,2026-11-01\n"
    ).encode()
    response = upload(client, data=data)
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["convertible"] is False
    assert payload["tickets"]["has_ticket_id"] is False
    confirm = client.post(
        f"/api/previews/{payload['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 422
    assert confirm.get_json()["error"] == "missing_ticket_id"
    assert client.get("/api/days/2026-11-01").get_json()["current_rev"] == 0


def test_blank_ticket_row_blocks_publish(client):
    data = make_csv([row("T1"), row(""), row("  "), row("T4")])
    response = upload(client, data=data)
    payload = response.get_json()
    assert payload["convertible"] is False
    assert payload["tickets"]["blank_row_count"] == 2
    assert [r["line"] for r in payload["tickets"]["blank_rows"]] == [3, 4]
    confirm = client.post(
        f"/api/previews/{payload['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 422
    assert confirm.get_json()["error"] == "blank_ticket_ids"


def test_cross_day_duplicate_is_rejected_whole_batch(client):
    preview_and_confirm(client, day="2026-11-01")
    # T2 already exists on 2026-11-01; publishing it again on another day grows
    # its total to two and must reject the entire batch.
    data = make_csv([row("X1"), row("T2"), row("X3")])
    response = upload(client, day="2026-11-02", data=data)
    payload = response.get_json()
    assert payload["convertible"] is False
    assert payload["tickets"]["cross_day_conflict_count"] == 1
    assert payload["tickets"]["cross_day_conflicts"][0]["ticket_id"] == "T2"

    confirm = client.post(
        f"/api/previews/{payload['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 422
    body = confirm.get_json()
    assert body["error"] == "duplicate_ticket"
    assert body["details"]["conflicts"][0]["ticket_id"] == "T2"
    # Nothing from the batch was published.
    day = client.get("/api/days/2026-11-02").get_json()
    assert day["current_rev"] == 0
    assert client.get("/download/2026-11-02/latest/original").status_code == 404


def test_same_ticket_twice_in_one_new_day_is_rejected(client):
    data = make_csv([row("T1"), row("T2"), row("T1")])
    payload = upload(client, day="2026-11-05", data=data).get_json()
    assert payload["convertible"] is False
    assert payload["tickets"]["in_day_duplicates"][0] == {"ticket_id": "T1", "count": 2}


def test_existing_duplicate_can_be_reduced_day_by_day(client):
    # Seed a legacy duplicate directly in the index (as an upgrade might find).
    preview_and_confirm(client, day="2026-11-01")
    from railapp import db

    conn = db.connect(client.application.config["DB_PATH"])
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO ticket_occurrences (day, rev, ticket_id, line_no) "
                "VALUES ('2026-11-01', 1, 'T9', 2), ('2026-11-01', 1, 'T9', 3)"
            )
    finally:
        conn.close()

    report = client.get("/api/tickets/duplicates").get_json()
    assert [d["ticket_id"] for d in report["duplicates"]] == ["T9"]
    assert report["duplicates"][0]["total"] == 2

    # Republishing the same day with one occurrence reduces the total to one
    # and is allowed; the count dropped, so the ticket is not even checked.
    data = make_csv([row("T9"), row("T4")])
    payload = upload(client, day="2026-11-01", data=data).get_json()
    assert payload["convertible"] is True
    confirm = client.post(
        f"/api/previews/{payload['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 201
    report = client.get("/api/tickets/duplicates").get_json()
    assert report["duplicate_count"] == 0


def test_unchanged_count_can_correct_other_fields(client):
    preview_and_confirm(client, day="2026-11-01")
    # Re-publishing the same day with the same ticket ids (new amounts) keeps
    # every per-day count unchanged, so the batch is allowed.
    data = make_csv([row("T1", amount="999"), row("T2", amount="1"), row("T3")])
    payload = upload(client, day="2026-11-01", data=data).get_json()
    assert payload["tickets"]["cross_day_conflict_count"] == 0
    assert payload["convertible"] is True
    confirm = client.post(
        f"/api/previews/{payload['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 201
    assert confirm.get_json()["rev"] == 2
    settlement = client.get("/download/2026-11-01/2/settlement").data.decode()
    assert "T1,P1,G1,999,2026-11-01" in settlement


def test_duplicate_page_lists_every_occurrence_with_line_numbers(client):
    preview_and_confirm(client, day="2026-11-01")
    # Introduce a duplicate across days with distinct remaining rows.
    data = make_csv([row("Z1"), row("Z2")])
    payload = upload(client, day="2026-11-02", data=data).get_json()
    preview_id = payload["preview_id"]
    client.post(f"/api/previews/{preview_id}/confirm", headers={"Accept": "application/json"})
    # Force a duplicate into the index to simulate legacy data.
    from railapp import db

    conn = db.connect(client.application.config["DB_PATH"])
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO ticket_occurrences (day, rev, ticket_id, line_no) "
                "VALUES ('2026-11-02', 1, 'T3', 4)"
            )
    finally:
        conn.close()

    report = client.get("/api/tickets/duplicates").get_json()
    group = report["duplicates"][0]
    assert group["ticket_id"] == "T3"
    assert group["total"] == 2
    assert sorted((o["day"], o["rev"], o["line_no"]) for o in group["occurrences"]) == [
        ("2026-11-01", 1, 4),
        ("2026-11-02", 1, 4),
    ]
    page = client.get("/duplicates")
    assert page.status_code == 200
    assert b"T3" in page.data


def test_revision_bump_replaces_index_occurrences(client):
    preview_and_confirm(client, day="2026-11-01")
    # Revision 2 drops T3 and adds T7.
    data = make_csv([row("T1"), row("T2"), row("T7")])
    payload = upload(client, day="2026-11-01", data=data).get_json()
    assert payload["convertible"] is True
    confirm = client.post(
        f"/api/previews/{payload['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 201
    assert confirm.get_json()["rev"] == 2

    report = client.get("/api/tickets/duplicates").get_json()
    assert report["days"][0]["rev"] == 2
    assert report["days"][0]["ticket_count"] == 3
    # T3 no longer exists in the current revision; T7 does.
    assert client.get("/api/tickets/duplicates?ticket_id=T3").get_json()["duplicates"] == []
    found = client.get("/api/tickets/duplicates?ticket_id=T7").get_json()["duplicates"]
    assert found[0]["occurrences"][0]["rev"] == 2


def test_confirm_reads_no_other_day_original(client, app):
    """Only the preview's own original is opened during confirmation."""
    preview_and_confirm(client, day="2026-11-01")
    data = make_csv([row("B1", travel="2026-11-02"), row("B2", travel="2026-11-02")])
    payload = upload(client, day="2026-11-02", data=data).get_json()

    store = app.config["STORE"]
    original_read = store.read_bytes
    reads = []

    def tracking_read(digest):
        reads.append(digest)
        return original_read(digest)

    store.read_bytes = tracking_read
    try:
        confirm = client.post(
            f"/api/previews/{payload['preview_id']}/confirm",
            headers={"Accept": "application/json"},
        )
    finally:
        store.read_bytes = original_read
    assert confirm.status_code == 201
    assert reads == [payload["original"]["sha256"]]


def test_upgrade_backfill_reports_missing_and_blank(client, app):
    """Simulate a legacy publication without an index row, then restart."""
    from railapp import db, tickets

    conn = db.connect(app.config["DB_PATH"])
    try:
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO publications (day, rev, original_name, original_size,
                   original_sha256, settlement_sha256, settlement_size, ridership_sha256,
                   ridership_size, settlement_fingerprint, ridership_fingerprint,
                   settlement_version, ridership_version, settlement_columns,
                   ridership_columns, row_count, created_at)
                   VALUES ('2026-01-01', 1, 'x.csv', 10, ?, ?, 10, ?, 10, 'f', 'f', 1, 1, '[]', '[]', 0, 'now')""",
                ("0" * 64, "0" * 64, "0" * 64),
            )
            conn.execute(
                "INSERT INTO days (day, current_rev, updated_at) VALUES ('2026-01-01', 1, 'now')"
            )
    finally:
        conn.close()

    conn = db.connect(app.config["DB_PATH"])
    try:
        report = tickets.backfill(conn, app.config["STORE"])
    finally:
        conn.close()
    assert report[0]["day"] == "2026-01-01"
    assert report[0]["state"] == "unavailable"
    payload = client.get("/api/tickets/duplicates").get_json()
    row = next(d for d in payload["days"] if d["day"] == "2026-01-01")
    assert row["state"] == "unavailable"


def test_backfill_marks_missing_column_and_blank_rows(client, app):
    from railapp import db, tickets

    data = (
        "passenger_id,train_no,amount,travel_date\n"
        "P1,G1,10,2026-11-01\n"
    ).encode()
    response = upload(client, data=data)
    payload = response.get_json()
    assert payload["tickets"]["has_ticket_id"] is False
    # The legacy revision cannot exist through normal publish (it is blocked),
    # so insert the publication row pair directly and backfill.
    conn = db.connect(app.config["DB_PATH"])
    try:
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO publications (day, rev, original_name, original_size,
                   original_sha256, settlement_sha256, settlement_size, ridership_sha256,
                   ridership_size, settlement_fingerprint, ridership_fingerprint,
                   settlement_version, ridership_version, settlement_columns,
                   ridership_columns, row_count, created_at)
                   VALUES ('2026-01-02', 1, 'x.csv', 10, ?, ?, 10, ?, 10, 'f', 'f', 1, 1, '[]', '[]', 0, 'now')""",
                (payload["original"]["sha256"], "0" * 64, "0" * 64),
            )
            conn.execute(
                "INSERT INTO days (day, current_rev, updated_at) VALUES ('2026-01-02', 1, 'now')"
            )
        tickets.backfill(conn, app.config["STORE"])
        index = db.get_day_ticket_index(conn, "2026-01-02")
    finally:
        conn.close()
    assert index["state"] == "missing_ticket_id"
    assert index["has_ticket_id"] == 0


def test_index_failure_rolls_back_publication(client, app, monkeypatch):
    """The new revision and its index must appear together or not at all."""
    from railapp import tickets

    preview_and_confirm(client, day="2026-11-01")

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated index failure")

    monkeypatch.setattr(tickets, "replace_day_index", boom)
    data = make_csv([row("NEW1"), row("NEW2")])
    payload = upload(client, day="2026-11-02", data=data).get_json()
    confirm = client.post(
        f"/api/previews/{payload['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 503
    assert client.get("/api/days/2026-11-02").get_json()["current_rev"] == 0
    # The old day keeps its old revision and old index.
    report = client.get("/api/tickets/duplicates").get_json()
    days = {d["day"]: d for d in report["days"]}
    assert days["2026-11-01"]["rev"] == 1
    assert "2026-11-02" not in days


def test_concurrent_same_new_ticket_across_days_only_one_wins(app):
    import threading

    client = app.test_client()
    data_a = make_csv([row("SAME", travel="2026-11-03"), row("A2", travel="2026-11-03")])
    data_b = make_csv([row("SAME", travel="2026-11-04"), row("B2", travel="2026-11-04")])
    first = upload(client, day="2026-11-03", data=data_a).get_json()
    second = upload(client, day="2026-11-04", data=data_b).get_json()
    assert first["convertible"] is True and second["convertible"] is True

    results = []
    barrier = threading.Barrier(2)

    def confirm(preview_id):
        local = app.test_client()
        barrier.wait()
        response = local.post(
            f"/api/previews/{preview_id}/confirm", headers={"Accept": "application/json"}
        )
        results.append((response.status_code, response.get_json()))

    threads = [
        threading.Thread(target=confirm, args=(first["preview_id"],)),
        threading.Thread(target=confirm, args=(second["preview_id"],)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    statuses = sorted(code for code, _ in results)
    assert statuses == [201, 422], results
    loss = next(payload for code, payload in results if code == 422)
    assert loss["error"] == "duplicate_ticket"
    assert loss["details"]["conflicts"][0]["ticket_id"] == "SAME"

    report = client.get("/api/tickets/duplicates").get_json()
    assert report["duplicate_count"] == 0
