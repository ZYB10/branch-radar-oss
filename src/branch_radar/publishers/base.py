from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, TypedDict

from ..database import Database


class PublicationOutcome(TypedDict, total=False):
    event_id: int
    status: str
    error: str
    warnings: list[Any]


class PublicationError(RuntimeError):
    """A destination refused or could not confirm a write.

    ``may_have_succeeded`` is the important half. A destination that cannot
    tell whether its write landed must say so, because the difference decides
    whether the delivery is retried or held for a human to reconcile.
    """

    def __init__(self, message: str, *, may_have_succeeded: bool = False):
        super().__init__(message)
        self.may_have_succeeded = may_have_succeeded


class PublicationTarget(ABC):
    """Where events go, and how that destination proves a write landed.

    This is the seam between *what* is published and *where*. `Publisher`
    owns leases, verification order and how an outcome is recorded, so every
    destination inherits the same durability rules; a target owns only the
    two operations that genuinely differ per destination.

    A target must not write to the database or decide a delivery's state.
    """

    #: Stored on the delivery row, so one event can reach several
    #: destinations without them colliding.
    destination_type: str

    @abstractmethod
    def contains(self, marker: str) -> bool:
        """Report whether ``marker`` is already present at the destination.

        This is what makes an interrupted publish safe to resume: it runs
        before the write to skip work already done, and after it to prove the
        write landed. A destination that cannot answer must raise
        `PublicationError` rather than guess.
        """

    @abstractmethod
    def append(self, delivery: dict[str, Any], marker: str) -> list[Any]:
        """Append one event, carrying ``marker`` so `contains` can find it.

        Returns destination warnings, which are advisory: they never make a
        delivery successful or failed on their own.
        """


class Publisher:
    """Move durable events to a destination without ever losing one.

    Sequencing, leases and the success/unknown decision live here so that a
    new destination cannot get them subtly wrong. A delivery is marked
    successful only once the marker is read back, so a destination that
    reports success it cannot prove still lands in ``unknown`` for a human
    rather than being silently dropped.
    """

    def __init__(self, database: Database, target: PublicationTarget, key: str):
        self.database = database
        self.target = target
        self.key = key

    @property
    def destination_type(self) -> str:
        return self.target.destination_type

    def queue_events(self, event_ids: Iterable[int]) -> None:
        for event_id in event_ids:
            event = self.database.get_event(event_id)
            if not event or event.get("kind") == "baseline":
                continue
            self.database.ensure_delivery(event_id, self.destination_type, self.key)

    def queue_all_events(self, *, limit: int = 1000) -> None:
        self.queue_events(
            self.database.list_events_without_delivery(
                self.destination_type, self.key, limit=limit
            )
        )

    def publish_pending(self, *, limit: int = 1000) -> list[PublicationOutcome]:
        outcomes: list[PublicationOutcome] = []
        for delivery in self.database.list_pending_deliveries(
            self.destination_type, self.key, limit=limit
        ):
            outcome = self._publish_one(delivery)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _publish_one(
        self, delivery: dict[str, Any]
    ) -> PublicationOutcome | None:
        delivery_id = int(delivery["id"])
        lease_token = self.database.claim_delivery(delivery_id)
        if not lease_token:
            return None
        event_id = delivery["event_id"]
        marker = "BRANCH-RADAR:%s" % delivery["event_key"]
        try:
            if self.target.contains(marker):
                self.database.mark_delivery_success(
                    delivery_id, "reconciled", lease_token=lease_token
                )
                return {"event_id": event_id, "status": "already_present"}
            warnings = self.target.append(delivery, marker)
        except PublicationError as exc:
            self.database.mark_delivery_error(
                delivery_id,
                str(exc),
                unknown=exc.may_have_succeeded,
                lease_token=lease_token,
            )
            return {
                "event_id": event_id,
                "status": "unknown" if exc.may_have_succeeded else "error",
                "error": str(exc),
            }

        # The write claimed success. Only reading the marker back proves it.
        try:
            verified = self.target.contains(marker)
        except PublicationError as exc:
            self.database.mark_delivery_error(
                delivery_id, str(exc), unknown=True, lease_token=lease_token
            )
            return {
                "event_id": event_id,
                "status": "unknown",
                "error": str(exc),
                "warnings": warnings,
            }
        if not verified:
            message = "目标返回成功，但回查没有找到事件标记"
            self.database.mark_delivery_error(
                delivery_id, message, unknown=True, lease_token=lease_token
            )
            return {
                "event_id": event_id,
                "status": "unknown",
                "error": message,
                "warnings": warnings,
            }
        self.database.mark_delivery_success(
            delivery_id, marker, lease_token=lease_token
        )
        return {"event_id": event_id, "status": "published", "warnings": warnings}
