"""Picks which OutreachChannel implementation to use, based on this tenant's own
`outreach_channel` Parameter -- defaults to HeyReach (the original, proven channel) if unset.
This is the one place that knows both channels exist; callers (autonomous orchestrator, the
manual outreach endpoint) never hardcode a specific channel."""

from sqlalchemy.orm import Session

from app.db.models import Parameter
from app.outreach.base import OutreachChannel
from app.outreach.heyreach import HeyReachChannel
from app.outreach.salesrobot import SalesRobotChannel


def get_outreach_channel(db: Session, tenant_id: int) -> OutreachChannel:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == "outreach_channel")
        .first()
    )
    channel_name = None
    if param and param.value:
        channel_name = param.value.get("value") if isinstance(param.value, dict) else param.value

    if channel_name == "salesrobot":
        return SalesRobotChannel(db, tenant_id)
    return HeyReachChannel(db, tenant_id)
