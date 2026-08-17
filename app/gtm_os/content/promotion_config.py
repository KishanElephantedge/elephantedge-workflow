"""Discovered-topic promotion configuration -- Step 16E-6, Batch 1 Part 1. Same Parameter-backed
storage pattern as topics.py/feeds.py (own key, own shape, same get/set/validate discipline, no
new mechanism invented).

CALIBRATION DEFAULTS, NOT SCIENTIFICALLY ESTABLISHED THRESHOLDS -- restated here, not just in the
design doc, because this is the file someone will actually read while tuning them. There is
currently ~zero real production TopicCandidate volume (Step 16E-4/16E-5 just shipped), so these
three numbers are deliberately conservative starting points, not derived from any observed
distribution. See topic-promotion-evidence-threshold-design.md Section 8/19/20 and this batch's
final report for the recalibration plan: run the sweep in dry-run mode against real accumulated
clusters first, look at what independent_entity_count/observation_span_days actually look like in
practice, then adjust -- not before."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

PROMOTION_CONFIG_PARAMETER_KEY = "content_intelligence_promotion_config"

# CALIBRATION DEFAULTS (see module docstring) -- deliberately conservative, not evidence-derived:
#   - min_independent_entities=3: comfortably above the hard independent_entity_count==1 floor
#     (Step 16E-6 design Section 8), so the configured minimum is doing real work rather than just
#     restating the floor.
#   - min_observations=3: same order of magnitude as min_independent_entities -- with no real
#     data yet, there's no basis for these two to differ, so they start equal.
#   - recent_window_days=30: roughly a month -- generous enough not to discard genuinely recurring
#     topics between sweep runs at this feature's current (low) sensing cadence, conservative
#     enough not to promote something that only ever happened once, long ago.
DEFAULT_PROMOTION_CONFIG: dict = {
    "min_independent_entities": 3,
    "min_observations": 3,
    "recent_window_days": 30,
}


class PromotionConfigError(ValueError):
    """Raised when promotion configuration fails validation -- never silently coerced."""


def _validate_promotion_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise PromotionConfigError("promotion config must be an object")
    for key in ("min_independent_entities", "min_observations", "recent_window_days"):
        if key not in config:
            raise PromotionConfigError(f"promotion config missing required key: {key!r}")
        value = config[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise PromotionConfigError(f"promotion config {key!r} must be a positive integer, got {value!r}")


def get_promotion_config(db: Session, tenant_id: int) -> dict:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == PROMOTION_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict):
        return param.value
    return DEFAULT_PROMOTION_CONFIG


def set_promotion_config(db: Session, tenant_id: int, config: dict) -> None:
    _validate_promotion_config(config)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == PROMOTION_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = config
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=PROMOTION_CONFIG_PARAMETER_KEY,
            value=config,
            description="Discovered ContentTopic promotion thresholds -- CALIBRATION DEFAULTS, "
            "not scientifically established (see topic-promotion-evidence-threshold-design.md "
            "and app/gtm_os/content/promotion_config.py)",
        )
        db.add(param)
    db.commit()
