"""GTM Motion configuration -- Batch 10. Same Parameter-backed storage pattern as
icp_config.py/offering_config.py -- own key, own shape, no new schema/CMS.

EXISTING CAPABILITY INSPECTED FIRST (Part 1's own instruction): business_context.py already has
a `gtm_motions` field -- `["Partnership motion", "Outbound motion", "LinkedIn content", "Inbound
/ SEO / webinars"]` (DEFAULT_BUSINESS_CONTEXT). This is real, but insufficient to serve as this
batch's configuration: it's a flat, unstructured list of strings (no applicable_icps/offerings,
no enabled/priority, and it already blends motions with channels -- "LinkedIn content" and
"Inbound / SEO / webinars" are channels/tactics under a motion, not motions themselves, exactly
the distinction Part 8 says to keep clean). Reused only as a REFERENCE point for the vocabulary
(confirms "outbound"/"inbound"/"partnerships" are real, lead-named concepts) -- not modified,
not treated as structured rule configuration.

DELIBERATELY UNCONFIGURED BY DEFAULT (unlike icp_config.py's DEFAULT_ICP_CONFIG, which WAS
seeded with real lead-given rules): the lead's reference names the four motions
(outbound/inbound/partnerships/events) but does NOT give an explicit ICP/offering -> motion
mapping the way it did for ICP -> offering (Batch 9). Per Part 13's own instruction ("do not
invent detailed rules for which ICP/offering should use which motion"), DEFAULT_GTM_MOTION_CONFIG
below seeds only the four real motion names with empty `applicable_icps`/`applicable_offerings`
-- exactly the same "seed the vocabulary, leave the rules for the lead to confirm" pattern
offering_config.py already established for offering qualification fields. This is what makes
gtm_motion.py honestly report `insufficient_context` for every company until real rules are
configured, rather than guessing."""

from sqlalchemy.orm import Session

from app.db.models import Parameter

GTM_MOTION_CONFIG_PARAMETER_KEY = "gtm_os_gtm_motion_config"

# The four real motion names from the lead's reference material -- reused, not re-invented.
GTM_MOTIONS = ("outbound", "inbound", "partnerships", "events")

DEFAULT_GTM_MOTION_CONFIG: list[dict] = [
    {
        "motion": motion,
        "enabled": True,
        "description": None,
        "applicable_icps": [],
        "applicable_offerings": [],
        "trigger_conditions": [],
        "priority": None,
        "rationale": None,
    }
    for motion in GTM_MOTIONS
]


class GtmMotionConfigError(ValueError):
    """Raised when GTM motion configuration fails validation -- never silently coerced."""


def _validate_gtm_motion_config(motions: list[dict]) -> None:
    if not isinstance(motions, list):
        raise GtmMotionConfigError("GTM motion config must be a list")
    seen_names: set[str] = set()
    for i, motion in enumerate(motions):
        if not isinstance(motion, dict):
            raise GtmMotionConfigError(f"motion at index {i} must be an object")
        name = motion.get("motion")
        if not isinstance(name, str) or not name.strip():
            raise GtmMotionConfigError(f"motion at index {i} has an empty or missing 'motion' name")
        if name in seen_names:
            raise GtmMotionConfigError(f"duplicate motion: {name!r}")
        seen_names.add(name)

        enabled = motion.get("enabled", True)
        if not isinstance(enabled, bool):
            raise GtmMotionConfigError(f"motion {name!r} has a malformed 'enabled' value -- must be true/false")

        for list_field in ("applicable_icps", "applicable_offerings", "trigger_conditions"):
            value = motion.get(list_field, [])
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise GtmMotionConfigError(f"motion {name!r} field {list_field!r} must be a list of strings")

        priority = motion.get("priority")
        if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool) or priority < 1):
            raise GtmMotionConfigError(f"motion {name!r} field 'priority' must be a positive integer or null")


def get_gtm_motion_config(db: Session, tenant_id: int) -> list[dict]:
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == GTM_MOTION_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param and isinstance(param.value, list):
        return param.value
    return DEFAULT_GTM_MOTION_CONFIG


def set_gtm_motion_config(db: Session, tenant_id: int, motions: list[dict]) -> None:
    _validate_gtm_motion_config(motions)
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == tenant_id)
        .filter(Parameter.key == GTM_MOTION_CONFIG_PARAMETER_KEY)
        .first()
    )
    if param:
        param.value = motions
    else:
        param = Parameter(
            tenant_id=tenant_id,
            key=GTM_MOTION_CONFIG_PARAMETER_KEY,
            value=motions,
            description="GTM Motion rules (applicable_icps/applicable_offerings/priority per "
            "motion) -- pending lead/CEO confirmation, see app/gtm_os/gtm_motion/gtm_motion_config.py",
        )
        db.add(param)
    db.commit()
