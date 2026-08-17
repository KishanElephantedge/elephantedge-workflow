"""Efficiency manual-time benchmark configuration -- same Parameter-backed storage pattern as
business_context.py/icp_config.py/offering_config.py/gtm_motion_config.py: own key, own shape,
no new schema/CMS, editable from V2 Settings without touching backend validation.

Each benchmark answers "how many minutes would a human take to do ONE unit of this activity by
hand" -- used by efficiency.py to convert real recorded activity volume into human-equivalent
time. Only the three benchmarks the lead's reference material explicitly names a number for
(contact enrichment, message drafting, CRM dedup check) are seeded with a real value below; every
other real activity_type this backend can measure real volume for is seeded manual_minutes=null,
honestly unconfigured, per the spec's explicit instruction not to invent a number. The
activity_type list here is kept in sync with efficiency.py's ACTIVITY_LABELS -- every activity
efficiency.py can produce volume for has a benchmark row (even if null), and no benchmark row
exists for an activity that isn't real."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

EFFICIENCY_BENCHMARK_PARAMETER_KEY = "gtm_os_efficiency_benchmarks"

# Kept in the same order as the reference deck's own breakdown table, then the remaining real
# activities this backend can measure. See efficiency.py's ACTIVITY_LABELS for what each
# activity_type actually means and where its volume comes from.
DEFAULT_EFFICIENCY_BENCHMARKS: list[dict] = [
    {"activity_type": "contact_enrichment", "label": "Contact enrichment", "manual_minutes": 5, "enabled": True},
    {"activity_type": "message_drafting", "label": "Message drafting", "manual_minutes": 4, "enabled": True},
    {"activity_type": "dedup_check", "label": "CRM dedup check", "manual_minutes": 2, "enabled": True},
    {"activity_type": "decision_maker_research", "label": "Decision-maker research", "manual_minutes": None, "enabled": True},
    {"activity_type": "hiring_signal_check", "label": "Hiring-signal check", "manual_minutes": None, "enabled": True},
    {"activity_type": "signal_monitoring", "label": "Signal monitoring", "manual_minutes": None, "enabled": True},
    {"activity_type": "company_discovery", "label": "Company discovery", "manual_minutes": None, "enabled": True},
]


class EfficiencyBenchmarkConfigError(ValueError):
    """Raised when benchmark configuration fails validation -- never silently coerced."""


def _validate_efficiency_benchmarks(benchmarks: list[dict]) -> None:
    if not isinstance(benchmarks, list):
        raise EfficiencyBenchmarkConfigError("Efficiency benchmark config must be a list")
    seen_types: set[str] = set()
    for i, b in enumerate(benchmarks):
        if not isinstance(b, dict):
            raise EfficiencyBenchmarkConfigError(f"benchmark at index {i} must be an object")
        activity_type = b.get("activity_type")
        if not isinstance(activity_type, str) or not activity_type.strip():
            raise EfficiencyBenchmarkConfigError(f"benchmark at index {i} has an empty or missing 'activity_type'")
        if activity_type in seen_types:
            raise EfficiencyBenchmarkConfigError(f"duplicate activity_type: {activity_type!r}")
        seen_types.add(activity_type)

        label = b.get("label")
        if not isinstance(label, str) or not label.strip():
            raise EfficiencyBenchmarkConfigError(f"benchmark {activity_type!r} has an empty or missing 'label'")

        manual_minutes = b.get("manual_minutes")
        if manual_minutes is not None and (not isinstance(manual_minutes, (int, float)) or isinstance(manual_minutes, bool) or manual_minutes < 0):
            raise EfficiencyBenchmarkConfigError(f"benchmark {activity_type!r} field 'manual_minutes' must be a non-negative number or null")

        enabled = b.get("enabled", True)
        if not isinstance(enabled, bool):
            raise EfficiencyBenchmarkConfigError(f"benchmark {activity_type!r} has a malformed 'enabled' value -- must be true/false")


def get_efficiency_benchmarks(db: Session, tenant_id: int) -> list[dict]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == EFFICIENCY_BENCHMARK_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, list):
        return param.value
    return DEFAULT_EFFICIENCY_BENCHMARKS


def set_efficiency_benchmarks(db: Session, tenant_id: int, benchmarks: list[dict]) -> None:
    _validate_efficiency_benchmarks(benchmarks)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == EFFICIENCY_BENCHMARK_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = benchmarks
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=EFFICIENCY_BENCHMARK_PARAMETER_KEY,
            value=benchmarks,
            description="Efficiency manual-time-per-activity benchmarks (minutes) -- see "
            "app/gtm_os/efficiency/benchmark_config.py",
        )
        db.add(param)
    db.commit()
