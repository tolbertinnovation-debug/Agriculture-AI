from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import ApiKey, Farmer, Organization, Role, utcnow
from ..security import constant_time_equals, hash_secret, verify_token

ROLE_RANK = {Role.viewer.value: 0, Role.officer.value: 1, Role.admin.value: 2}


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not constant_time_equals(x_admin_token, get_settings().admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


@dataclass
class Principal:
    org: Organization
    key: ApiKey

    @property
    def role(self) -> str:
        return self.key.role


def get_principal(x_api_key: str | None = Header(default=None), db: Session = Depends(get_db)) -> Principal:
    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing X-API-Key")
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_secret(x_api_key), ApiKey.revoked_at.is_(None)))
    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
    key.last_used_at = utcnow()
    db.commit()
    return Principal(org=key.org, key=key)


def role_at_least(minimum: str):
    def dep(p: Principal = Depends(get_principal)) -> Principal:
        if ROLE_RANK.get(p.role, -1) < ROLE_RANK[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires {minimum} role")
        return p

    return dep


viewer = role_at_least(Role.viewer.value)
officer = role_at_least(Role.officer.value)
admin = role_at_least(Role.admin.value)


def current_farmer(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> Farmer:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    payload = verify_token(authorization.split(" ", 1)[1].strip())
    if not payload or payload.get("typ") != "farmer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    farmer = db.get(Farmer, payload.get("fid"))
    if farmer is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "farmer not found")
    return farmer


def org_farmer(db: Session, org: Organization, farmer_id: int) -> Farmer:
    farmer = db.get(Farmer, farmer_id)
    if farmer is None or farmer.org_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "farmer not found")
    return farmer
