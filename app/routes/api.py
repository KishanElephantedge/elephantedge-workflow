from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import Batch, Credential, Parameter
from app.db.session import get_db

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
        "companies": [],  # no discovery logic built yet -- see ARCHITECTURE.md status checklist
    }


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
