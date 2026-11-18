"""Authentication (a dev fixture) and authorization (not a fixture).

Auth is static bearer tokens from the seed, stored as SHA-256 digests. Replacing it with OIDC
would touch only this file.

Three scopes, no overlap:
  user            participants and organizers, pinned to one organization
  judge           the worker plane under /internal/judge/*, never minted for a person
  platform_admin  bootstrap: create organizations and identities

Cross-tenant reads answer 404 rather than 403, so an id cannot be used to probe for existence.
A wrong role inside your own organization answers 403.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_session
from .domain.enums import Role, Scope, TokenKind
from .domain.errors import AuthenticationError, AuthorizationError
from .models import ApiToken, User

TOKEN_PREFIX = "shodh_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(label: str) -> str:
    return f"{TOKEN_PREFIX}{label}_{secrets.token_urlsafe(24)}"


@dataclass(frozen=True)
class Principal:
    token_id: uuid.UUID
    scope: str
    label: str
    user_id: uuid.UUID | None = None
    org_id: uuid.UUID | None = None
    role: Role | None = None
    email: str | None = None

    @property
    def is_organizer(self) -> bool:
        return self.role is Role.ORGANIZER

    @property
    def is_participant(self) -> bool:
        return self.role is Role.PARTICIPANT

    def log_fields(self) -> dict:
        return {
            "actor_id": str(self.user_id) if self.user_id else None,
            "actor_scope": self.scope,
            "actor_org": str(self.org_id) if self.org_id else None,
            "actor_role": self.role.value if self.role else None,
        }


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise AuthenticationError("missing bearer token")
    token = header[7:].strip()
    if not token:
        raise AuthenticationError("empty bearer token")
    return token


def current_principal(
    request: Request, session: Session = Depends(get_session)
) -> Principal:
    token = _bearer(request)
    row = session.execute(
        select(ApiToken).where(
            ApiToken.token_sha256 == hash_token(token), ApiToken.revoked_at.is_(None)
        )
    ).scalar_one_or_none()
    if row is None:
        raise AuthenticationError("unknown or revoked token")
    user: User | None = None
    if row.kind is TokenKind.USER:
        user = session.get(User, row.user_id)
        if user is None:
            raise AuthenticationError("token identity no longer exists")
    principal = Principal(
        token_id=row.id,
        scope=row.scope,
        label=row.label,
        user_id=user.id if user else None,
        org_id=user.org_id if user else None,
        role=user.role if user else None,
        email=user.email if user else None,
    )
    request.state.principal = principal
    return principal


def require_user(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.scope != Scope.USER.value or principal.user_id is None:
        raise AuthorizationError("this endpoint requires a user identity")
    return principal


def require_organizer(principal: Principal = Depends(require_user)) -> Principal:
    if not principal.is_organizer:
        raise AuthorizationError("organizer role required")
    return principal


def require_participant(principal: Principal = Depends(require_user)) -> Principal:
    if not principal.is_participant:
        raise AuthorizationError("participant role required")
    return principal


def require_judge(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.scope != Scope.JUDGE.value:
        raise AuthorizationError("judge service credential required")
    return principal


def require_platform_admin(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.scope != Scope.PLATFORM_ADMIN.value:
        raise AuthorizationError("platform admin credential required")
    return principal
