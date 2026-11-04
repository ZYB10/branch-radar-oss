import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from branch_radar.database import Database
from branch_radar.models import ChangeFacts, RepositoryCandidate
from branch_radar.publisher import LarkPublisher
from branch_radar.publishers import (
    MarkdownFileTarget,
    PublicationError,
    PublicationTarget,
    Publisher,
)
from branch_radar.rules import summarize_with_rules


class FakeLark:
    def __init__(self):
        self.marker_checks = 0
        self.appended = []

    def contains_marker(self, document, marker):
        self.marker_checks += 1
        return self.marker_checks > 1

    def append_xml(self, document, content):
        self.appended.append((document, content))
        return {"ok": True, "data": {"result": "success", "warnings": ["minor"]}}

    @staticmethod
    def warnings(envelope):
        return list((envelope.get("data") or {}).get("warnings") or [])


class PublisherRecoveryTests(unittest.TestCase):
    def test_publish_pending_passes_hard_limit_to_database(self):
        database = mock.Mock()
        database.list_pending_deliveries.return_value = []
        publisher = LarkPublisher(database, FakeLark(), "doc")

        self.assertEqual(publisher.publish_pending(limit=7), [])

        database.list_pending_deliveries.assert_called_once_with(
            "lark_doc", "doc", limit=7
        )

    def test_existing_event_without_delivery_is_queued_and_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "state.db")
            database.initialize()
            repository_id = database.upsert_repository(RepositoryCandidate(
                name="app",
                path=str(root / "app"),
                remote_name="origin",
                remote_url="https://git.example.com/app.git",
                canonical_remote="git.example.com/app",
                current_branch="main",
                upstream_branch="main",
                default_branch="main",
            ))
            branch_id = database.add_branch(
                repository_id, "main", "origin", baseline_oid="a" * 40
            )
            facts = ChangeFacts(
                repository_name="app",
                repository_path=str(root / "app"),
                remote_url="https://git.example.com/app.git",
                branch="main",
                event_kind="fast_forward",
                old_sha="a" * 40,
                new_sha="b" * 40,
                merge_base="a" * 40,
                detected_at="2026-08-18T00:00:00+00:00",
                categories=["feature"],
            )
            event_id, _key = database.create_event_and_advance(
                branch_id,
                facts,
                summarize_with_rules(facts),
                expected_sequence=0,
            )
            self.assertEqual(database.list_pending_deliveries("lark_doc", "doc"), [])

            lark = FakeLark()
            publisher = LarkPublisher(database, lark, "doc")
            publisher.queue_all_events(limit=100)
            outcomes = publisher.publish_pending()

            self.assertEqual(outcomes[0]["event_id"], event_id)
            self.assertEqual(outcomes[0]["status"], "published")
            self.assertEqual(outcomes[0]["warnings"], ["minor"])
            self.assertEqual(len(lark.appended), 1)
            self.assertEqual(database.list_pending_deliveries("lark_doc", "doc"), [])


def _seed_event(root: Path) -> tuple:
    database = Database(root / "state.db")
    database.initialize()
    repository_id = database.upsert_repository(RepositoryCandidate(
        name="app",
        path=str(root / "app"),
        remote_name="origin",
        remote_url="https://git.example.com/app.git",
        canonical_remote="git.example.com/app",
        current_branch="main",
        upstream_branch="main",
        default_branch="main",
    ))
    branch_id = database.add_branch(
        repository_id, "main", "origin", baseline_oid="a" * 40
    )
    facts = ChangeFacts(
        repository_name="app",
        repository_path=str(root / "app"),
        remote_url="https://user:secret@git.example.com/app.git",
        branch="main",
        event_kind="fast_forward",
        old_sha="a" * 40,
        new_sha="b" * 40,
        merge_base="a" * 40,
        detected_at="2026-08-18T00:00:00+00:00",
        categories=["feature"],
    )
    event_id, _key = database.create_event_and_advance(
        branch_id, facts, summarize_with_rules(facts), expected_sequence=0
    )
    return database, event_id


class MarkdownTargetTests(unittest.TestCase):
    """A second destination is the only real proof the seam holds."""

    def test_markdown_target_publishes_and_stays_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, event_id = _seed_event(root)
            destination = root / "out" / "feed.md"
            target = MarkdownFileTarget(destination)
            publisher = Publisher(database, target, str(destination))

            publisher.queue_all_events(limit=100)
            outcomes = publisher.publish_pending()
            self.assertEqual(outcomes[0]["event_id"], event_id)
            self.assertEqual(outcomes[0]["status"], "published")

            body = destination.read_text(encoding="utf-8")
            self.assertIn("app / main", body)
            self.assertIn("BRANCH-RADAR:", body)
            # Same rule as the Lark renderer: credentials never reach output.
            self.assertNotIn("secret", body)

            # Nothing is left pending, and a re-queue does not duplicate.
            self.assertEqual(
                database.list_pending_deliveries("markdown_file", str(destination)),
                [],
            )
            publisher.queue_all_events(limit=100)
            self.assertEqual(publisher.publish_pending(), [])
            self.assertEqual(body, destination.read_text(encoding="utf-8"))

    def test_a_write_that_cannot_be_verified_is_unknown_not_success(self):
        class LyingTarget(PublicationTarget):
            destination_type = "markdown_file"

            def contains(self, marker):
                return False

            def append(self, delivery, marker):
                return []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, event_id = _seed_event(root)
            publisher = Publisher(database, LyingTarget(), "key")
            publisher.queue_all_events(limit=100)
            outcomes = publisher.publish_pending()

            # The target claimed success; the marker read-back did not agree.
            self.assertEqual(outcomes[0]["status"], "unknown")
            self.assertEqual(outcomes[0]["event_id"], event_id)

    def test_an_unreadable_destination_never_looks_empty(self):
        class UnreadableTarget(PublicationTarget):
            destination_type = "markdown_file"

            def __init__(self):
                self.appends = 0

            def contains(self, marker):
                raise PublicationError("目标不可读")

            def append(self, delivery, marker):
                self.appends += 1
                return []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, _event_id = _seed_event(root)
            target = UnreadableTarget()
            publisher = Publisher(database, target, "key")
            publisher.queue_all_events(limit=100)
            outcomes = publisher.publish_pending()

            # Unreadable is not absent: appending would have duplicated.
            self.assertEqual(outcomes[0]["status"], "error")
            self.assertEqual(target.appends, 0)

    def test_markdown_target_reports_an_unreadable_file_rather_than_appending(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "feed.md"
            destination.write_text("existing\n", encoding="utf-8")
            destination.chmod(0o000)
            target = MarkdownFileTarget(destination)
            try:
                with self.assertRaises(PublicationError):
                    target.contains("BRANCH-RADAR:anything")
            except AssertionError:
                if os.geteuid() == 0:
                    self.skipTest("root reads regardless of mode")
                raise
            finally:
                # Restore before the temporary directory is torn down.
                destination.chmod(0o600)

    def test_missing_file_is_absent_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            target = MarkdownFileTarget(Path(directory) / "never-written.md")
            self.assertFalse(target.contains("BRANCH-RADAR:anything"))


if __name__ == "__main__":
    unittest.main()
