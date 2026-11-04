from __future__ import annotations

from typing import Any

from ..integrations.lark_cli import LarkCli, LarkError, render_event_xml
from .base import PublicationError, PublicationTarget


class LarkDocumentTarget(PublicationTarget):
    """Append events to a Lark document through the user's own `lark-cli`.

    Credentials stay with `lark-cli` and its keychain entry; nothing about the
    Lark session is read or stored here.
    """

    destination_type = "lark_doc"

    def __init__(self, lark: LarkCli, document: str):
        self.lark = lark
        self.document = document

    def contains(self, marker: str) -> bool:
        try:
            return self.lark.contains_marker(self.document, marker)
        except LarkError as exc:
            raise PublicationError(
                str(exc), may_have_succeeded=exc.may_have_succeeded
            ) from exc

    def append(self, delivery: dict[str, Any], marker: str) -> list[Any]:
        try:
            envelope = self.lark.append_xml(
                self.document, render_event_xml(delivery)
            )
        except LarkError as exc:
            raise PublicationError(
                str(exc), may_have_succeeded=exc.may_have_succeeded
            ) from exc
        return self.lark.warnings(envelope)
