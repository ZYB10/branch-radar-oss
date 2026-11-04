"""Backwards-compatible name for the Lark publishing path.

The publishing side is now split the way the observation side is: `Publisher`
owns leases and verification, a `PublicationTarget` owns one destination. See
`branch_radar.publishers`.
"""

from __future__ import annotations

from .database import Database
from .integrations.lark_cli import LarkCli
from .publishers import LarkDocumentTarget, PublicationOutcome, Publisher


class LarkPublisher(Publisher):
    def __init__(self, database: Database, lark: LarkCli, document: str):
        super().__init__(database, LarkDocumentTarget(lark, document), document)


__all__ = ["LarkPublisher", "PublicationOutcome"]
