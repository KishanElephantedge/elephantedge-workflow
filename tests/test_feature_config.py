"""Per-partner features as configuration, not code.

The rule being pinned: a new partner wanting an existing feature must be data entry -- enable the
flag, fill the config -- never a code change and a deploy. Before this, email reporting was
hardcoded to one tenant id with a hardcoded list of campaign ids.
"""
import pytest

from app.db.models import Credential, Parameter, Tenant
from app.gtm_os.features import config as fc
from app.gtm_os.features import registry as F

ELEPHANT_EDGE, PARTNER_A, PARTNER_B = 2, 15, 77


@pytest.fixture
def db(db_factory):
    db = db_factory([Tenant, Parameter, Credential])
    for tid, name in ((ELEPHANT_EDGE, "Elephant Edge"), (PARTNER_A, "Partner A"), (PARTNER_B, "Partner B")):
        db.add(Tenant(id=tid, name=name, slug=name.lower().replace(" ", "-")))
    db.commit()
    return db


def test_enabling_a_feature_is_data_not_code(db):
    fc.set_enabled_features(db, PARTNER_B, ["accounts", "email_campaigns"])
    assert fc.enabled_features(db, PARTNER_B) == ["accounts", "email_campaigns"]
    assert fc.enabled_features(db, PARTNER_A) == []          # untouched


def test_a_typo_in_a_feature_name_is_rejected_rather_than_silently_stored(db):
    # Otherwise it sits in the column looking enabled and does nothing at all.
    with pytest.raises(ValueError, match="unknown feature"):
        fc.set_enabled_features(db, PARTNER_B, ["accounts", "webinarz"])
    assert fc.enabled_features(db, PARTNER_B) == []


def test_two_partners_configure_the_same_feature_differently(db):
    """The case that previously required hardcoding: same feature, different campaigns."""
    fc.set_config(db, PARTNER_A, "email_campaigns",
                  {"smartlead_campaign_ids": [{"id": 4037761, "label": "Cost Saving Angle"}]})
    fc.set_config(db, PARTNER_B, "email_campaigns",
                  {"smartlead_campaign_ids": [{"id": 999, "label": "B's Campaign"}]})

    assert fc.get_config(db, PARTNER_A, "email_campaigns")["smartlead_campaign_ids"][0]["id"] == 4037761
    assert fc.get_config(db, PARTNER_B, "email_campaigns")["smartlead_campaign_ids"][0]["id"] == 999


def test_config_updates_are_partial_so_a_form_cannot_wipe_a_field_it_does_not_know(db):
    # The partner ICP save is a full replace, and that is exactly how a field got wiped once.
    fc.set_config(db, PARTNER_A, "content", {"own_linkedin_profile_url": "https://linkedin.com/in/x"})
    fc.set_config(db, PARTNER_A, "content", {"partner_content_context": {"voice": "direct"}})
    config = fc.get_config(db, PARTNER_A, "content")
    assert config["own_linkedin_profile_url"] == "https://linkedin.com/in/x"
    assert config["partner_content_context"] == {"voice": "direct"}


def test_an_unknown_config_key_is_rejected(db):
    with pytest.raises(ValueError, match="no config key"):
        fc.set_config(db, PARTNER_A, "content", {"not_a_real_key": "x"})


def test_readiness_names_exactly_what_is_missing_before_a_run(db):
    fc.set_enabled_features(db, PARTNER_A, ["email_campaigns"])
    status = fc.status(db, PARTNER_A, "email_campaigns")
    assert status.enabled is True
    assert status.ready is False
    assert status.missing_config == ["smartlead_campaign_ids"]
    assert status.missing_credentials == ["smartlead_api_key"]

    fc.set_config(db, PARTNER_A, "email_campaigns", {"smartlead_campaign_ids": [{"id": 1, "label": "x"}]})
    db.add(Credential(tenant_id=PARTNER_A, name="smartlead_api_key", value="sk-real"))
    db.commit()

    status = fc.status(db, PARTNER_A, "email_campaigns")
    assert (status.ready, status.missing_config, status.missing_credentials) == (True, [], [])


def test_a_feature_needing_no_config_is_ready_once_enabled(db):
    fc.set_enabled_features(db, PARTNER_A, ["webinars"])
    assert fc.status(db, PARTNER_A, "webinars").ready is True


def test_access_is_gated_on_the_flag_not_a_tenant_id(db):
    """The whole point: any partner can have the feature; nobody is special-cased."""
    fc.set_enabled_features(db, PARTNER_B, ["email_campaigns"])
    fc.require_enabled(db, PARTNER_B, "email_campaigns")      # does not raise
    with pytest.raises(LookupError):
        fc.require_enabled(db, PARTNER_A, "email_campaigns")


def test_every_declared_feature_reports_a_status_for_any_partner(db):
    statuses = fc.all_statuses(db, PARTNER_B)
    assert {s.key for s in statuses} == set(F.feature_keys())
    assert all(s.enabled is False for s in statuses)          # new partner starts with nothing on
