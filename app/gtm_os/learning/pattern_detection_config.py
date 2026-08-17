"""Candidate-pattern detection configuration -- same Parameter-backed storage pattern as
business_context.py/icp_config.py/offering_config.py/gtm_motion_config.py/benchmark_config.py.

Two knobs, both detection-sensitivity settings, not business facts: `min_occurrences` (N) and
`lookback_days` (M). The reference material gives no specific number for either, so these are
seeded with a plainly-labeled technical default (occurrence-threshold tuning, the same category
as e.g. a page-size constant elsewhere in this codebase) rather than a business claim -- fully
editable, never presented as a lead-confirmed rule."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

PATTERN_DETECTION_CONFIG_PARAMETER_KEY = "gtm_os_pattern_detection_config"

DEFAULT_PATTERN_DETECTION_CONFIG: dict = {
    "min_occurrences": 3,
    "lookback_days": 30,
}


class PatternDetectionConfigError(ValueError):
    """Raised when pattern-detection configuration fails validation -- never silently coerced."""


def _validate_pattern_detection_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise PatternDetectionConfigError("Pattern detection config must be an object")
    min_occurrences = config.get("min_occurrences")
    if not isinstance(min_occurrences, int) or isinstance(min_occurrences, bool) or min_occurrences < 1:
        raise PatternDetectionConfigError("'min_occurrences' must be a positive integer")
    lookback_days = config.get("lookback_days")
    if not isinstance(lookback_days, int) or isinstance(lookback_days, bool) or lookback_days < 1:
        raise PatternDetectionConfigError("'lookback_days' must be a positive integer")


def get_pattern_detection_config(db: Session, tenant_id: int) -> dict:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == PATTERN_DETECTION_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict):
        return param.value
    return DEFAULT_PATTERN_DETECTION_CONFIG


def set_pattern_detection_config(db: Session, tenant_id: int, config: dict) -> None:
    _validate_pattern_detection_config(config)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == PATTERN_DETECTION_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = config
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=PATTERN_DETECTION_CONFIG_PARAMETER_KEY,
            value=config,
            description="Candidate-pattern detection sensitivity (min_occurrences, lookback_days) "
            "-- see app/gtm_os/learning/pattern_detection_config.py",
        )
        db.add(param)
    db.commit()
