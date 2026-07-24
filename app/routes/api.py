from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.models import Batch, Company, Credential, Parameter
from app.db.session import get_db
from app.heyreach_client import HeyReachError
from app.outreach.heyreach import HeyReachChannel
from app.phases.campaign_execution import run_campaign_execution
from app.phases.decision_maker import run_decision_maker_id

router = APIRouter()

# Elephant Edge's tenant_id in the shared tenants table (see synefi's Tenant row for
# slug="elephant-edge"). Hardcoded here deliberately: this backend only ever serves this one
# tenant -- it is not a second multi-tenant backend, it IS Elephant Edge's dedicated backend.
# If Elephant Edge's tenant_id ever changes, update this constant.
ELEPHANT_EDGE_TENANT_ID = 2


# ---- Batches ----
# Generic batch CRUD only -- no phase-execution endpoints yet. Phase 1 (ICP) is blocked on
# Gokul's confirmation call (see ARCHITECTURE.md); building Discovery/Qualification/etc.
# endpoints ahead of a confirmed ICP would be exactly the kind of premature implementation
# this project's own "Discovery is not Qualification" discipline argues against.

@router.post("/batches")
def create_batch(name: str, db: Session = Depends(get_db)):
    batch = Batch(tenant_id=ELEPHANT_EDGE_TENANT_ID, name=name)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return {"id": batch.id, "name": batch.name, "status": batch.status}


@router.get("/batches")
def list_batches(db: Session = Depends(get_db)):
    batches = (
        db.query(Batch)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .order_by(Batch.created_at.desc())
        .all()
    )
    return [
        {
            "id": b.id,
            "name": b.name,
            "created_at": b.created_at,
            "current_phase": b.current_phase,
            "status": b.status,
            "company_count": len(b.companies),
        }
        for b in batches
    ]


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return {
        "id": batch.id,
        "name": batch.name,
        "current_phase": batch.current_phase,
        "status": batch.status,
        "companies": [
            {
                "id": c.id,
                "name": c.name,
                "domain": c.domain,
                "contact_count": len(c.contacts),
                "decision_maker_searched": c.decision_maker_searched_at is not None,
            }
            for c in batch.companies
        ],
    }


# ---- Manual company import (hand-picked companies, Discovery/Qualification skipped) ----
# For a batch like the 10 companies the team lead sent directly -- these are already
# hand-vetted, so Phase 2 (Discovery) and Phase 3 (Qualification) don't apply. Companies
# are seeded straight in, ready for Phase 6 (Decision Maker) to run against them.

class CompanyImport(BaseModel):
    name: str
    domain: str


@router.post("/batches/{batch_id}/companies/import")
def import_companies(batch_id: int, companies: list[CompanyImport], db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    created = []
    for c in companies:
        company = Company(batch_id=batch_id, name=c.name, domain=c.domain)
        db.add(company)
        created.append(company)
    batch.current_phase = "companies_imported"
    db.commit()
    for c in created:
        db.refresh(c)
    return {"imported": len(created), "companies": [{"id": c.id, "name": c.name, "domain": c.domain} for c in created]}


# ---- Phase 6: Decision Maker Intelligence ----

@router.post("/batches/{batch_id}/phases/decision-maker")
def execute_decision_maker_id(batch_id: int, retry_company_ids: list[int] | None = Body(default=None), db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    result = run_decision_maker_id(batch_id, db, retry_company_ids=retry_company_ids)
    batch.current_phase = "decision_maker_done"
    db.commit()
    return result


# ---- Phase 11: Campaign Execution ----
# The channel is chosen here, at the call site -- HeyReachChannel is passed in as an
# OutreachChannel implementation; run_campaign_execution itself has no idea HeyReach exists.

@router.post("/batches/{batch_id}/phases/outreach")
def execute_outreach_push(batch_id: int, db: Session = Depends(get_db)):
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id)
        .filter(Batch.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    channel = HeyReachChannel(db, ELEPHANT_EDGE_TENANT_ID)
    try:
        result = run_campaign_execution(batch_id, db, channel)
    except HeyReachError as e:
        raise HTTPException(status_code=400, detail=str(e))
    batch.current_phase = "outreach_done"
    db.commit()
    return result


# ---- Credentials (Settings page) ----

@router.get("/credentials")
def list_credentials(db: Session = Depends(get_db)):
    creds = db.query(Credential).filter(Credential.tenant_id == ELEPHANT_EDGE_TENANT_ID).all()
    return [{"name": c.name, "is_set": bool(c.value), "updated_at": c.updated_at} for c in creds]


@router.post("/credentials")
def upsert_credential(name: str, value: str, db: Session = Depends(get_db)):
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Credential.name == name)
        .first()
    )
    if cred:
        cred.value = value
    else:
        cred = Credential(tenant_id=ELEPHANT_EDGE_TENANT_ID, name=name, value=value)
        db.add(cred)
    db.commit()
    return {"name": name, "is_set": True}


@router.delete("/credentials/{name}")
def delete_credential(name: str, db: Session = Depends(get_db)):
    cred = (
        db.query(Credential)
        .filter(Credential.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Credential.name == name)
        .first()
    )
    if cred:
        db.delete(cred)
        db.commit()
    return {"deleted": name}


# ---- Parameters (ICP config, once Phase 1 unblocks) ----

@router.get("/parameters")
def list_parameters(db: Session = Depends(get_db)):
    params = db.query(Parameter).filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID).all()
    return [{"key": p.key, "value": p.value, "description": p.description} for p in params]


@router.post("/parameters")
def upsert_parameter(key: str, value: dict, description: str = "", db: Session = Depends(get_db)):
    param = (
        db.query(Parameter)
        .filter(Parameter.tenant_id == ELEPHANT_EDGE_TENANT_ID)
        .filter(Parameter.key == key)
        .first()
    )
    if param:
        param.value = value
        param.description = description or param.description
    else:
        param = Parameter(tenant_id=ELEPHANT_EDGE_TENANT_ID, key=key, value=value, description=description)
        db.add(param)
    db.commit()
    return {"key": key, "value": value}
