from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TypedDict

from ..models import ChangeFacts


class Watch(TypedDict, total=False):
    """Typed projection of the database row consumed by fact sources.

    The database may add presentation-only columns over time, while source and
    monitor code rely on this stable subset. ``total=False`` reflects rows built
    by test doubles as well as older databases; required fields are still read
    with ``[]`` at their point of use so malformed rows fail loudly.
    """

    branch_id: int
    repository_id: int
    repository: str
    repository_alias: str | None
    repository_tags: list[str]
    local_path: str
    remote_name: str | None
    remote_fingerprint: str | None
    branch: str
    last_observed_oid: str | None
    last_present_oid: str | None
    observation_sequence: int
    present: bool
    enabled: bool
    last_checked_at: str | None
    last_success_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class BranchTransition:
    """Cursor state the monitor already holds, handed to a source to explain.

    The monitor owns sequencing and durability; a source owns everything that
    depends on how the branch is observed, including how a move is classified.
    """

    old_sha: str | None
    previous_tip: str | None
    new_sha: str | None
    was_present: bool
    sequence: int

    @property
    def was_seen_before(self) -> bool:
        return self.sequence > 0

    @property
    def is_deletion(self) -> bool:
        return self.new_sha is None and self.old_sha is not None

    @property
    def is_reappearance(self) -> bool:
        return (
            self.new_sha is not None
            and self.old_sha is None
            and not self.was_present
            and self.was_seen_before
        )


class FactSource(ABC):
    """Where branch facts come from.

    Local checkouts are one answer; a server-side mirror or a forge webhook is
    another. Everything downstream of `describe` — rule summaries, the event
    log, the outbox, Lark publishing — is already source-agnostic, so keeping
    the observation seam explicit is what lets a second source exist at all.

    Implementations must never advance a cursor or write to the database.
    """

    name: str = "unknown"

    def refresh(
        self, watches: list[Watch], *, fetch: bool = True
    ) -> dict[int, str]:
        """Prepare for a round and report branches that must not be checked.

        Returns blocking errors keyed by tracked branch id. A branch listed
        here is skipped entirely: no observation, no cursor movement. Refusing
        is always safer than guessing, because a wrong answer here silently
        rewrites history for the branch.
        """
        return {}

    @abstractmethod
    def resolve(self, watch: Watch) -> str | None:
        """Return the current tip SHA, or None when the branch is absent."""

    @abstractmethod
    def describe(self, watch: Watch, transition: BranchTransition) -> ChangeFacts:
        """Classify the transition and gather the evidence behind it.

        Must set `event_kind`, and must set `analysis_complete=False` rather
        than inventing evidence it cannot actually reconstruct.
        """

    def attach_patch_excerpt(
        self, watch: Watch, facts: ChangeFacts, *, max_bytes: int
    ) -> None:
        """Optionally attach a redacted diff excerpt for AI summarization.

        Only ever called under an explicit `--codex` opt-in. Sources that
        cannot produce a trustworthy excerpt should leave it empty.
        """
        return None
