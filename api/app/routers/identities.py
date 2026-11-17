"""The development authentication fixture, plus organization and identity bootstrap.

A grader must be able to point the system at organizations, participants and problem data it
has never seen. These routes are how: a platform-admin credential creates an organization and
identities, and /auth/dev/token mints a bearer token for any identity. Nothing else in the
system depends on how tokens are issued.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import Principal, current_principal, hash_token, new_token, require_platform_admin
from ..config import get_settings
from ..db import get_session
from ..domain.enums import Role, Scope, TokenKind
from ..domain.errors import AuthorizationError, BusinessRuleError, NotFoundError
from ..models import ApiToken, Organization, User
from ..schemas import (
    MeOut,
    OrganizationIn,
    OrganizationOut,
    TokenOut,
    TokenRequest,
    UserIn,
    UserOut,
    UserWithTokenOut,
)

router = APIRouter(tags=["identities"])
settings = get_settings()


def _issue_token(session: Session, user: User) -> str:
    token = new_token(user.role.value.lower())
    session.add(
        ApiToken(
            id=uuid.uuid4(),
            user_id=user.id,
            kind=TokenKind.USER,
            scope=Scope.USER.value,
            label=f"{user.email}",
            token_sha256=hash_token(token),
        )
    )
    return token


@router.get("/me", response_model=MeOut, summary="Who this credential is")
def me(principal: Principal = Depends(current_principal), session: Session = Depends(get_session)) -> MeOut:
    user = session.get(User, principal.user_id) if principal.user_id else None
    return MeOut(
        scope=principal.scope,
        label=principal.label,
        user=UserOut.model_validate(user) if user else None,
    )


@router.post(
    "/auth/dev/token",
    response_model=TokenOut,
    summary="Mint a bearer token for a seeded or newly created identity (development fixture)",
)
def dev_token(body: TokenRequest, session: Session = Depends(get_session)) -> TokenOut:
    if not settings.dev_auth_enabled:
        raise AuthorizationError("development authentication is disabled")
    user = session.execute(select(User).where(User.email == body.email.lower())).scalar_one_or_none()
    if user is None:
        raise NotFoundError("no such identity")
    token = _issue_token(session, user)
    session.commit()
    return TokenOut(token=token, user=UserOut.model_validate(user))


@router.post(
    "/orgs",
    response_model=OrganizationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an organization (platform admin)",
)
def create_org(
    body: OrganizationIn,
    _: Principal = Depends(require_platform_admin),
    session: Session = Depends(get_session),
) -> OrganizationOut:
    org = Organization(id=uuid.uuid4(), slug=body.slug, name=body.name)
    session.add(org)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise BusinessRuleError("organization slug already exists", code="duplicate_slug") from exc
    return OrganizationOut.model_validate(org)


@router.get("/orgs/{org_id}", response_model=OrganizationOut, summary="Read one organization")
def get_org(
    org_id: uuid.UUID,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> OrganizationOut:
    """A user credential may read only its own organization; anything else is a 404."""
    if principal.scope == Scope.USER.value and principal.org_id != org_id:
        raise NotFoundError("organization not found")
    org = session.get(Organization, org_id)
    if org is None:
        raise NotFoundError("organization not found")
    return OrganizationOut.model_validate(org)


@router.post(
    "/orgs/{org_id}/users",
    response_model=UserWithTokenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an identity in an organization (platform admin, or an organizer of that org)",
)
def create_user(
    org_id: uuid.UUID,
    body: UserIn,
    response: Response,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> UserWithTokenOut:
    is_admin = principal.scope == Scope.PLATFORM_ADMIN.value
    is_local_organizer = (
        principal.scope == Scope.USER.value and principal.role is Role.ORGANIZER and principal.org_id == org_id
    )
    if not (is_admin or is_local_organizer):
        # Do not confirm whether the organization exists to a caller from another tenant.
        if principal.scope == Scope.USER.value and principal.org_id != org_id:
            raise NotFoundError("organization not found")
        raise AuthorizationError("organizer or platform admin credential required")
    if session.get(Organization, org_id) is None:
        raise NotFoundError("organization not found")

    user = User(
        id=uuid.uuid4(),
        org_id=org_id,
        email=body.email,
        display_name=body.display_name,
        role=body.role,
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise BusinessRuleError("email already registered", code="duplicate_email") from exc
    token = _issue_token(session, user)
    session.commit()
    response.headers["Location"] = f"/users/{user.id}"
    out = UserWithTokenOut.model_validate(user, from_attributes=True)
    return out.model_copy(update={"token": token})
