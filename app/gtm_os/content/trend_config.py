"""Trend Intelligence configuration -- Batch 2 Part F/L. Same Parameter-backed storage pattern as
promotion_config.py/topics.py/feeds.py (own key, own shape, same get/set/validate discipline).

CALIBRATION DEFAULTS, NOT SCIENTIFICALLY ESTABLISHED THRESHOLDS -- same honesty requirement as
promotion_config.py, restated here for the same reason: this is the file someone tuning trend
behavior will actually read. There is essentially zero real trend-relevant history yet (Batch 1's
promotion layer only just started being able to create discovered topics at all), so these
numbers are conservative starting points pending real observation, not derived thresholds."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

TREND_CONFIG_PARAMETER_KEY = "content_intelligence_trend_config"

# CALIBRATION DEFAULTS (see module docstring):
#   - window_days=14: used as the length of BOTH the "recent" and "previous" comparison windows
#     (recent = last 14 days, previous = the 14 days before that) -- one number doing double duty
#     rather than two independently-guessed values with nothing yet to justify them differing
#     (Part F's own "simple interpretable time-window comparison" instruction).
#   - min_recent_observations=2 / min_recent_independent_entities=2: the floor below which a
#     recent window is treated as having nothing meaningful to say (insufficient_evidence) --
#     deliberately low (barely above the promotion layer's own hard independent_entity_count==1
#     floor) since this is a reporting/description layer, not a gate; a real signal shouldn't be
#     called "insufficient" just because the bar was set high with no data behind it.
#   - acceleration_ratio=2.0: recent >= previous * 2 is called "accelerating"; recent <= previous
#     / 2 is called "declining" -- a round, conservative multiplier (not "3 vs 2 observations"
#     being treated as statistically meaningful, per gpt.txt's own explicit warning), chosen only
#     because SOME ratio must exist to draw the accelerating/declining vs stable/persistent line
#     interpretably.
#   - persistent_span_days=30: the ALL-TIME (not just recent-vs-previous) evidence span above
#     which a topic with a steady (non-accelerating, non-declining) ratio is called "persistent"
#     rather than merely "stable" -- roughly one window-pair's worth of history beyond the
#     comparison windows themselves, so "persistent" means genuinely longer-lived, not just
#     "steady for 4 weeks."
DEFAULT_TREND_CONFIG: dict = {
    "window_days": 14,
    "min_recent_observations": 2,
    "min_recent_independent_entities": 2,
    "acceleration_ratio": 2.0,
    "persistent_span_days": 30,
}


class TrendConfigError(ValueError):
    """Raised when trend configuration fails validation -- never silently coerced."""


def _validate_trend_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise TrendConfigError("trend config must be an object")
    int_keys = ("window_days", "min_recent_observations", "min_recent_independent_entities", "persistent_span_days")
    for key in int_keys:
        if key not in config:
            raise TrendConfigError(f"trend config missing required key: {key!r}")
        value = config[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise TrendConfigError(f"trend config {key!r} must be a positive integer, got {value!r}")
    if "acceleration_ratio" not in config:
        raise TrendConfigError("trend config missing required key: 'acceleration_ratio'")
    ratio = config["acceleration_ratio"]
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or ratio <= 1:
        raise TrendConfigError(f"trend config 'acceleration_ratio' must be a number > 1, got {ratio!r}")


def get_trend_config(db: Session, tenant_id: int) -> dict:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == TREND_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, dict):
        return param.value
    return DEFAULT_TREND_CONFIG


def set_trend_config(db: Session, tenant_id: int, config: dict) -> None:
    _validate_trend_config(config)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == TREND_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = config
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=TREND_CONFIG_PARAMETER_KEY,
            value=config,
            description="Trend Intelligence time-window/classification thresholds -- CALIBRATION "
            "DEFAULTS, not scientifically established (see app/gtm_os/content/trend_config.py)",
        )
        db.add(param)
    db.commit()
