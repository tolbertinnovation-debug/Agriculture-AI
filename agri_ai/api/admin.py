"""Platform operator endpoints: onboard partner organisations and run jobs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..jobs import JOBS, run_jobs
from ..models import ApiKey, Organization, Role
from ..security import generate_api_key
from . import schemas as s
from .deps import require_admin

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/orgs", response_model=s.OrgCreated, status_code=201)
def create_org(body: s.OrgCreate, db: Session = Depends(get_db)):
    for field in ("sms_shortcode", "ussd_code"):
        value = getattr(body, field)
        if value and db.scalar(select(Organization.id).where(getattr(Organization, field) == value)):
            raise HTTPException(409, f"{field} already assigned to another organisation")
    org = Organization(**body.model_dump())
    db.add(org)
    db.flush()
    plaintext, prefix, digest = generate_api_key()
    db.add(ApiKey(org_id=org.id, name="initial admin key", role=Role.admin.value, prefix=prefix, key_hash=digest))
    db.commit()
    return s.OrgCreated(org=s.OrgOut.model_validate(org), api_key=plaintext)


@router.get("/orgs", response_model=list[s.OrgOut])
def list_orgs(db: Session = Depends(get_db)):
    return db.scalars(select(Organization).order_by(Organization.id)).all()


@router.post("/jobs/run")
def run_all_jobs(jobs: list[str] = Query(list(JOBS)), db: Session = Depends(get_db)):
    unknown = set(jobs) - set(JOBS)
    if unknown:
        raise HTTPException(422, f"unknown jobs: {sorted(unknown)}")
    return run_jobs(db, tuple(jobs))
