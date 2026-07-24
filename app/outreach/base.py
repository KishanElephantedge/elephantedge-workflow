from abc import ABC, abstractmethod

from app.db.models import Contact


class OutreachChannel(ABC):
    """A pluggable send target for Campaign Execution (Phase 11). Swapping HeyReach for a
    different tool, or adding a second channel (email, etc.) alongside it, means writing a
    new implementation of this interface -- the orchestration logic in
    app/phases/campaign_execution.py never changes to accommodate a new channel."""

    @abstractmethod
    def push_lead(self, contact: Contact) -> dict:
        """Attempt to send this one contact through this channel.

        Returns: {"status": "pushed" | "failed" | "skipped", "error_message": str | None,
        "channel_ref": str | None} -- channel_ref is whatever identifier the channel used
        (e.g. a campaign ID), stored for audit, not interpreted by the caller.
        """
        ...
