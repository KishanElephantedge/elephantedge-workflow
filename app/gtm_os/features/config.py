"""Reading and writing a partner's enabled features and their per-feature configuration.

Everything here is data: `tenants.enabled_features` for which modules a partner gets, and
Parameter rows for what those modules are pointed at. No feature logic branches on tenant id.

`readiness()` is the part that earns its keep. Before this, a feature was switched on and then
failed at runtime -- or worse, half-worked -- because something it needed was never set. Now the
question "is this partner actually ready to use Webinars?" is answerable from data, before a run,
and the answer names exactly what is missing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import Parameter, Tenant
from app.gtm_os.features import registry as F


@dataclass
class FeatureStatus:
    key: str
    label: str
    description: str
    enabled: bool
    ready: bool
    missing_config: list[str]
    missing_credentials: list[str]
    config: dict
    note: str | None = None


def enabled_features(db: Session, tenant_id: int) -> list[str]:
    tenant = db.get(Tenant, tenant_id)
    value = getattr(tenant, "enabled_features", None) if tenant else None
    if isinstance(value, str):          # tolerated: the column is JSON but has been written as text
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [str(v) for v in value] if isinstance(value, list) else []


def set_enabled_features(db: Session, tenant_id: int, features: list[str]) -> list[str]:
    """Turn features on or off for one partner. Unknown keys are rejected rather than stored --
    a typo'd feature name would otherwise sit in the column looking enabled and do nothing."""
    unknown = [f for f in features if F.get_feature(f) is None]
    if unknown:
        raise ValueError(f"unknown feature(s): {', '.join(sorted(unknown))}. "
                         f"Known: {', '.join(F.feature_keys())}")
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise LookupError(f"tenant {tenant_id} not found")
    tenant.enabled_features = list(dict.fromkeys(features))     # de-duped, order preserved
    db.commit()
    return tenant.enabled_features


def get_config(db: Session, tenant_id: int, feature_key: str) -> dict:
    feature = F.get_feature(feature_key)
    if feature is None:
        raise ValueError(f"unknown feature: {feature_key}")
    keys = [c.key for c in feature.config]
    if not keys:
        return {}
    rows = (db.query(Parameter)
            .filter(Parameter.tenant_id == tenant_id, Parameter.key.in_(keys)).all())
    return {r.key: r.value for r in rows}


def set_config(db: Session, tenant_id: int, feature_key: str, values: dict) -> dict:
    """Write per-tenant settings for one feature.

    PARTIAL UPDATE, deliberately: only the keys supplied are written. The partner ICP save is a
    full replace and that has already caused real data loss once -- a form that didn't know about
    a field wiped it on save. Config is edited from more than one place, so it merges."""
    feature = F.get_feature(feature_key)
    if feature is None:
        raise ValueError(f"unknown feature: {feature_key}")
    allowed = {c.key: c for c in feature.config}
    unknown = [k for k in values if k not in allowed]
    if unknown:
        raise ValueError(f"{feature_key} has no config key(s): {', '.join(sorted(unknown))}")

    for key, value in values.items():
        row = (db.query(Parameter)
               .filter(Parameter.tenant_id == tenant_id, Parameter.key == key).first())
        if row is None:
            row = Parameter(tenant_id=tenant_id, key=key, value=value,
                            description=f"{feature.label}: {allowed[key].label}")
            db.add(row)
        else:
            row.value = value
    db.commit()
    return get_config(db, tenant_id, feature_key)


def _missing_credentials(db: Session, tenant_id: int, feature: F.Feature) -> list[str]:
    if not feature.credentials:
        return []
    from app.db.models import Credential

    present = {c.name for c in db.query(Credential).filter(Credential.tenant_id == tenant_id).all()
               if c.value}
    return [k for k in feature.credentials if k not in present]


def status(db: Session, tenant_id: int, feature_key: str) -> FeatureStatus:
    feature = F.get_feature(feature_key)
    if feature is None:
        raise ValueError(f"unknown feature: {feature_key}")
    config = get_config(db, tenant_id, feature_key)
    missing_config = [c.key for c in feature.config
                      if c.required and not config.get(c.key)]
    missing_credentials = _missing_credentials(db, tenant_id, feature)
    return FeatureStatus(
        key=feature.key,
        label=feature.label,
        description=feature.description,
        enabled=feature_key in enabled_features(db, tenant_id),
        ready=not missing_config and not missing_credentials,
        missing_config=missing_config,
        missing_credentials=missing_credentials,
        config=config,
        note=feature.note,
    )


def all_statuses(db: Session, tenant_id: int) -> list[FeatureStatus]:
    """Every feature the platform offers, and where this partner stands on each. This is what the
    admin screen renders, and it is also the onboarding checklist."""
    return [status(db, tenant_id, f.key) for f in F.FEATURES]


def require_enabled(db: Session, tenant_id: int, feature_key: str) -> None:
    """Gate a partner-facing route on the feature flag instead of on a tenant id.

    Raises LookupError, which routes translate to a 404 -- the same shape the hardcoded
    _require_majji_tenant() produced, so a partner without the feature still cannot tell it exists.
    """
    if feature_key not in enabled_features(db, tenant_id):
        raise LookupError(f"{feature_key} is not enabled for tenant {tenant_id}")
