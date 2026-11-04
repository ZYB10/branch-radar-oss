#!/usr/bin/env python3
"""Repeatable synthetic benchmark for the local dashboard read path."""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from branch_radar.database import Database

REPOSITORIES = 40
BRANCHES_PER_REPOSITORY = 20
EVENTS = 8_000
ROUNDS = 25


def populate(database: Database) -> None:
    now = "2026-08-20T00:00:00+00:00"
    commits = [
        {
            "sha": "%040x" % index,
            "parents": ["a" * 40],
            "author_name": "Author %d" % (index % 8),
            "author_email": "redacted@example.invalid",
            "author_date": now,
            "committer_name": "Author %d" % (index % 8),
            "committer_email": "redacted@example.invalid",
            "commit_date": now,
            "subject": "Synthetic change %d" % index,
        }
        for index in range(40)
    ]
    facts = json.dumps(
        {
            "analysis_complete": True,
            "commit_count": len(commits),
            "commits": commits,
            "dropped_commits": [],
            "diff": {
                "files_changed": 100,
                "insertions": 500,
                "deletions": 120,
                "files": ["src/file_%03d.py" % index for index in range(100)],
            },
        },
        separators=(",", ":"),
    )
    summary = json.dumps(
        {
            "title": "Synthetic dashboard event",
            "overview": "Representative summary used to compare compact and full reads.",
            "categories": ["backend"],
            "highlights": ["Keeps benchmark payload realistic."],
            "risks": [],
            "verification": ["Run unit tests."],
            "deployment": {"impact": "none", "notes": []},
            "confidence": "high",
            "source": "rules",
        },
        separators=(",", ":"),
    )
    authors = json.dumps(["Author %d" % index for index in range(8)])
    branch_count = REPOSITORIES * BRANCHES_PER_REPOSITORY
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO repositories(
                id, name, local_path, enabled, created_at, last_seen_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            """,
            [
                (index, "repo-%03d" % index, "/benchmark/repo-%03d" % index, now, now)
                for index in range(1, REPOSITORIES + 1)
            ],
        )
        connection.executemany(
            """
            INSERT INTO tracked_branches(
                id, repository_id, name, enabled, present, observation_sequence,
                last_observed_oid, last_checked_at, created_at
            ) VALUES (?, ?, ?, 1, 1, ?, ?, ?, ?)
            """,
            [
                (
                    branch_id,
                    ((branch_id - 1) // BRANCHES_PER_REPOSITORY) + 1,
                    "branch-%02d" % ((branch_id - 1) % BRANCHES_PER_REPOSITORY),
                    EVENTS // branch_count,
                    "b" * 40,
                    now,
                    now,
                )
                for branch_id in range(1, branch_count + 1)
            ],
        )
        connection.executemany(
            """
            INSERT INTO branch_events(
                id, event_key, tracked_branch_id, sequence, kind, from_oid,
                to_oid, detected_at, facts_json, authors_json, summary_json,
                summary_source
            ) VALUES (?, ?, ?, ?, 'fast_forward', ?, ?, ?, ?, ?, ?, 'rules')
            """,
            [
                (
                    event_id,
                    "BR-benchmark-%d" % event_id,
                    ((event_id - 1) % branch_count) + 1,
                    ((event_id - 1) // branch_count) + 1,
                    "a" * 40,
                    "b" * 40,
                    now,
                    facts,
                    authors,
                    summary,
                )
                for event_id in range(1, EVENTS + 1)
            ],
        )


def measure(operation) -> float:
    samples = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "state.db")
        database.initialize()
        populate(database)

        def separate_full_reads() -> None:
            database.overview_counts()
            database.list_watches()
            database.list_events(limit=41, include_baseline=False)

        def separate_compact_reads() -> None:
            database.overview_counts()
            database.list_watches()
            database.list_events(limit=41, include_baseline=False, compact=True)

        def snapshot_read() -> None:
            database.dashboard_snapshot(event_limit=41)

        full_events = database.list_events(limit=41, include_baseline=False)
        compact_events = database.list_events(
            limit=41, include_baseline=False, compact=True
        )
        full_bytes = len(json.dumps(full_events, separators=(",", ":")))
        compact_bytes = len(json.dumps(compact_events, separators=(",", ":")))

        print("Synthetic dataset: %d repositories, %d branches, %d events" % (
            REPOSITORIES, REPOSITORIES * BRANCHES_PER_REPOSITORY, EVENTS
        ))
        print("Separate full reads:    %.2f ms median" % measure(separate_full_reads))
        print("Separate compact reads: %.2f ms median" % measure(separate_compact_reads))
        print("Single snapshot read:   %.2f ms median" % measure(snapshot_read))
        print("Event materialization:  %d -> %d bytes (%.1f%% smaller)" % (
            full_bytes,
            compact_bytes,
            (1 - compact_bytes / full_bytes) * 100,
        ))


if __name__ == "__main__":
    main()
