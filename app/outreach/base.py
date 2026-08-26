from abc import ABC, abstractmethod

from app.db.models import Contact


class OutreachChannel(ABC):
    """A pluggable send target for Campaign Execution (Phase 11). Swapping HeyReach for a
    different tool, or adding a second channel (email, etc.) alongside it, means writing a
    new implementation of this interface -- the orchestration logic in
    app/phases/campaign_execution.py never changes to accommodate a new channel."""

    @abstractmethod
    def push_lead(self, contact: Contact, offering_name: str | None = None) -> dict:
        """Attempt to send this one contact through this channel.

        offering_name: the offering this contact's batch was tagged with (Batch.offering_name),
        or None for a legacy/untagged batch. When given, implementations must route to THAT
        offering's own configured campaign (app/gtm_os/opportunity/offering_config.py's
        get_offering_campaign_id) -- never silently fall back to this tenant's single default
        campaign, since that would misroute a lead into the wrong offering's campaign. When
        None, implementations fall back to the tenant's single default campaign, unchanged from
        before per-offering routing existed.

        Returns: {"status": "pushed" | "failed" | "skipped", "error_message": str | None,
        "channel_ref": str | None} -- channel_ref is whatever identifier the channel used
        (e.g. a campaign ID), stored for audit, not interpreted by the caller.
        """
        ...
