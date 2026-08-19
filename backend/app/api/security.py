"""Authentication for the FastAPI control plane (Phase 16).

The API ships with authentication *architecture* in place -- a single
dependency (`get_current_principal`) that every route already depends on
-- but no auth is enforced by default (`AHSEA_REQUIRE_API_KEY=false`),
matching a local-dev/demo posture. Turning on auth is a config change,
not a code change: set `AHSEA_REQUIRE_API_KEY=true` and
`AHSEA_API_KEYS=key1,key2`, and every route under `app.api.routers.projects`
starts rejecting unauthenticated requests with 401, with zero route-handler
changes required.

Swapping the *scheme* (e.g. to JWT bearer tokens or OAuth2) later only
means replacing this module's internals -- `get_current_principal`'s
signature and the `Principal` it returns are the only thing routers and
services depend on.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Header, HTTPException, status
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class APISecuritySettings(BaseSettings):
    require_api_key: bool = False
    #: Comma-separated in the environment, e.g. AHSEA_API_KEYS=devkey1,devkey2
    api_keys: str = ""

    model_config = SettingsConfigDict(env_prefix="AHSEA_", env_file=".env", extra="ignore")

    @property
    def valid_keys(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache
def get_security_settings() -> APISecuritySettings:
    return APISecuritySettings()


class Principal(BaseModel):
    """Whoever (or whatever) is making the request."""

    subject: str
    authenticated: bool


def get_current_principal(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    settings = get_security_settings()

    if not settings.require_api_key:
        return Principal(subject="anonymous", authenticated=False)

    if not x_api_key or x_api_key not in settings.valid_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return Principal(subject=x_api_key, authenticated=True)
