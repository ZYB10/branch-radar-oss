from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

from .config import AppConfig
from .database import ConcurrentUpdate, Database
from .git import GitClient, GitError
from .models import ChangeFacts, CheckResult, Summary
from .rules import summarize_with_rules
from .security import redact_sensitive_text
from .sources import BranchTransition, FactSource, GitFactSource, Watch

# Neither kind carries a diff or a commit range, so a model call would add
# latency and a data hand-off without adding information the rules missed.
_KINDS_WITHOUT_COMPARISON = frozenset({"baseline", "deleted"})


class SummaryGenerator(Protocol):
    def summarize(self, facts: ChangeFacts) -> Summary:
        ...


class BranchMonitor:
    """Turn observations into durable events.

    Deliberately knows nothing about Git: what a branch move means is the
    `FactSource`'s job, while sequencing, cursor advancement and the AI
    hand-off stay here so every source gets identical durability guarantees.
    """

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        *,
        source: FactSource | None = None,
        git: GitClient | None = None,
        codex_summarizer: SummaryGenerator | None = None,
    ):
        self.database = database
        self.config = config
        self.source = source or GitFactSource(config, git=git or GitClient())
        self.codex_summarizer = codex_summarizer

    def check_all(
        self,
        *,
        fetch: bool = True,
        use_codex: bool = False,
        check_run_id: str | None = None,
    ) -> list[CheckResult]:
        return self.check_watches(
            cast(list[Watch], self.database.list_watches()),
            fetch=fetch,
            use_codex=use_codex,
            check_run_id=check_run_id,
        )

    def check_watches(
        self,
        watches: Sequence[Watch],
        *,
        fetch: bool = True,
        use_codex: bool = False,
        check_run_id: str | None = None,
    ) -> list[CheckResult]:
        watches = list(watches)
        blocked = self.source.refresh(watches, fetch=fetch)
        results: list[CheckResult] = []
        for watch in watches:
            message = blocked.get(int(watch["branch_id"]))
            if message:
                self._record_check_error(
                    watch,
                    message,
                    check_run_id=check_run_id,
                    blocked=True,
                )
                results.append(self._error_result(watch, message, blocked=True))
                continue
            results.append(
                self._check_one(
                    watch,
                    use_codex=use_codex,
                    check_run_id=check_run_id,
                )
            )
        return results

    def _check_one(
        self,
        watch: Watch,
        *,
        use_codex: bool,
        check_run_id: str | None,
    ) -> CheckResult:
        branch = str(watch["branch"])
        branch_id = int(watch["branch_id"])
        old_sha = str(watch["last_observed_oid"]) if watch.get("last_observed_oid") else None
        previous_tip = str(watch["last_present_oid"]) if watch.get("last_present_oid") else None
        sequence = int(watch.get("observation_sequence") or 0)
        was_present = bool(watch.get("present"))
        try:
            new_sha = self.source.resolve(watch)

            if new_sha is not None and new_sha == old_sha:
                self.database.touch_unchanged(
                    branch_id,
                    expected_sequence=sequence,
                    expected_oid=old_sha,
                    check_run_id=check_run_id,
                )
                return CheckResult(
                    str(watch["repository"]),
                    branch,
                    "unchanged",
                    new_sha=new_sha,
                    branch_id=branch_id,
                )

            if new_sha is None and old_sha is None:
                # A branch that was already known to be gone stays gone; only a
                # branch that never resolved at all is a configuration error.
                if not was_present and sequence > 0:
                    self.database.touch_unchanged(
                        branch_id,
                        present=False,
                        expected_sequence=sequence,
                        expected_oid=old_sha,
                        check_run_id=check_run_id,
                    )
                    return CheckResult(
                        str(watch["repository"]),
                        branch,
                        "unchanged",
                        event_kind="deleted",
                        branch_id=branch_id,
                    )
                message = "未找到分支 %s" % branch
                self.database.set_check_error(
                    branch_id,
                    message,
                    expected_sequence=sequence,
                    expected_oid=old_sha,
                    check_run_id=check_run_id,
                )
                return self._error_result(watch, message)

            transition = BranchTransition(
                old_sha=old_sha,
                previous_tip=previous_tip,
                new_sha=new_sha,
                was_present=was_present,
                sequence=sequence,
            )
            facts = self.source.describe(watch, transition)
            summary = summarize_with_rules(facts)
            event_id, _event_key = self.database.create_event_and_advance(
                branch_id,
                facts,
                summary,
                expected_sequence=sequence,
                check_run_id=check_run_id,
            )

            warning = None
            if self._codex_applies(facts.event_kind, use_codex):
                summary, warning = self._summarize_with_codex(
                    watch, facts, event_id, summary
                )

            result = CheckResult(
                str(watch["repository"]),
                branch,
                "baseline" if facts.event_kind == "baseline" else "changed",
                event_id,
                facts.event_kind,
                previous_tip if facts.event_kind == "recreated" else old_sha,
                new_sha,
                summary,
                branch_id=branch_id,
            )
            result.warning = warning
            return result
        except ConcurrentUpdate as exc:
            return self._error_result(watch, str(exc))
        except (GitError, OSError) as exc:
            message = redact_sensitive_text(str(exc))
            self._record_check_error(watch, message, check_run_id=check_run_id)
            return self._error_result(watch, message)

    def _codex_applies(self, kind: str, use_codex: bool) -> bool:
        return (
            use_codex
            and self.codex_summarizer is not None
            and kind not in _KINDS_WITHOUT_COMPARISON
        )

    def _summarize_with_codex(
        self,
        watch: Watch,
        facts: ChangeFacts,
        event_id: int,
        fallback: Summary,
    ) -> tuple[Summary, str | None]:
        summarizer = self.codex_summarizer
        assert summarizer is not None
        try:
            self.source.attach_patch_excerpt(
                watch, facts, max_bytes=self.config.patch_excerpt_bytes
            )
            summary = summarizer.summarize(facts)
            self.database.save_summary(event_id, summary)
            return summary, None
        except Exception as exc:
            # The event already holds the deterministic summary from the
            # cursor transaction, so a failed model call costs context, not data.
            return fallback, "Codex 摘要失败，已降级为规则摘要：%s" % str(exc)

    def _record_check_error(
        self,
        watch: Watch,
        message: str,
        *,
        check_run_id: str | None = None,
        blocked: bool = False,
    ) -> None:
        safe_message = redact_sensitive_text(message)
        local_path = str(watch.get("local_path") or "")
        if local_path:
            safe_message = safe_message.replace(local_path, "[repository]")
        try:
            self.database.set_check_error(
                int(watch["branch_id"]),
                safe_message,
                expected_sequence=int(watch.get("observation_sequence") or 0),
                expected_oid=(
                    str(watch["last_observed_oid"])
                    if watch.get("last_observed_oid")
                    else None
                ),
                check_run_id=check_run_id,
                check_run_status="blocked" if blocked else "error",
            )
        except ConcurrentUpdate:
            pass

    @staticmethod
    def _error_result(
        watch: Watch, message: str, *, blocked: bool = False
    ) -> CheckResult:
        return CheckResult(
            repository=str(watch["repository"]),
            branch=str(watch["branch"]),
            status="error",
            error=message,
            branch_id=int(watch["branch_id"]),
            blocked=blocked,
        )
