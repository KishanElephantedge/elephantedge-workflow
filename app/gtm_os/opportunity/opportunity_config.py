"""Opportunity eligibility configuration -- Batch 4 Part D. Same Parameter-backed storage
pattern as promotion_config.py/trend_config.py (own key, own shape, same get/set/validate
discipline).

CALIBRATION DEFAULTS, NOT SCIENTIFICALLY ESTABLISHED THRESHOLDS -- same honesty requirement as
promotion_config.py/trend_config.py. There is currently zero real Opportunity-relevant volume
(DemandHypothesis itself has near-zero real rows as of Batch 3's activation), so these are
conservative starting points, not derived thresholds."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

OPPORTUNITY_CONFIG_PARAMETER_KEY = "gtm_os_opportunity_config"

# CALIBRATION DEFAULTS (see module docstring):
#   - min_independent_evidence=2: DemandHypothesis.confidence.independent_evidence_count must be
#     at least this many -- one qualifying observation alone (independent_evidence_count=1, the
#     minimum for a DemandHypothesis to exist at all) is treated as real but not yet enough to
#     call an account an actionable Opportunity; a second independent observation is the smallest
#     step up from "exists" to "worth acting on," pending real data to justify anything higher.
#   - recent_window_days=60: account-level evidence is naturally sparser than market-level
#     evidence (one company, not many independent publishers/authors), so this window is wider
#     than Content Intelligence's own promotion/trend windows (30/14 days) -- a deliberate,
#     documented difference, not an oversight.
DEFAULT_OPPORTUNITY_CONFIG: dict = {
    "min_independent_evidence": 2,
    "recent_window_days": 60,
}


class OpportunityConfigError(ValueError):
    """Raised when opportunity configuration fails validation -- never silently coerced."""


def _validate_opportunity_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise OpportunityConfigError("opportunity config must be an object")
    for key in ("min_independent_evidence", "recent_window_days"):
        if key not in config:
            raise OpportunityConfigError(f"opportunity config missing required key: {key!r}")
        value = config[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise OpportunityConfigError(f"opportunity config {key!r} must be a positive integer, got {value!r}")


def get_opportunity_config(db: Session, tenant_id: int) -> dict:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == OPPORTUNITY_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict):
        return param.value
    return DEFAULT_OPPORTUNITY_CONFIG


def set_opportunity_config(db: Session, tenant_id: int, config: dict) -> None:
    _validate_opportunity_config(config)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == OPPORTUNITY_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = config
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=OPPORTUNITY_CONFIG_PARAMETER_KEY,
            value=config,
            description="Opportunity eligibility thresholds -- CALIBRATION DEFAULTS, not "
            "scientifically established (see app/gtm_os/opportunity/opportunity_config.py)",
        )
        db.add(param)
    db.commit()
