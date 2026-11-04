"""Destinations for durable branch events.

`Publisher` owns leases, verification and outcome recording; a
`PublicationTarget` owns only what differs per destination. The split mirrors
`FactSource`: durability rules stay in one place so a new destination cannot
get them subtly wrong.
"""

from .base import (
    PublicationError,
    PublicationOutcome,
    PublicationTarget,
    Publisher,
)
from .lark_doc import LarkDocumentTarget
from .markdown_file import MarkdownFileTarget, default_markdown_path

__all__ = [
    "LarkDocumentTarget",
    "MarkdownFileTarget",
    "PublicationError",
    "PublicationOutcome",
    "PublicationTarget",
    "Publisher",
    "default_markdown_path",
]
