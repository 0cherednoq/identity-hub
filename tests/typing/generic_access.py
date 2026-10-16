"""Static contract for schema-derived credential and session types (S11.2)."""

from __future__ import annotations

from typing import assert_type

from pydantic import BaseModel

from identity_hub import LoginContext, Pool, Service, SpaceHandle


class Credentials(BaseModel):
    username: str


class Session(BaseModel):
    token: str


class TotpSecret(BaseModel):
    seed: str


SERVICE = Service(
    "typing-contract",
    "Typing contract",
    credentials=Credentials,
    session=Session,
    credential_kinds={"totp": TotpSecret},
)
assert_type(SERVICE, Service[Credentials, Session])


async def _lease_contract(space: SpaceHandle) -> None:
    pool = space.pool(SERVICE)
    assert_type(pool, Pool[Credentials, Session])
    async with pool.lease() as lease:
        assert_type(await lease.credentials(), Credentials | None)
        assert_type(await lease.active_credential(), Credentials | None)
        assert_type(await lease.session(), Session | None)
        assert_type(
            await lease.credential("totp", schema=TotpSecret),
            TotpSecret | None,
        )


async def _login_contract(ctx: LoginContext[Credentials, Session]) -> Session:
    assert_type(await ctx.credentials(), Credentials | None)
    assert_type(await ctx.active_credential(), Credentials | None)
    assert_type(await ctx.session(), Session | None)
    assert_type(await ctx.credential("totp", schema=TotpSecret), TotpSecret | None)
    return Session(token="ok")


UNTYPED = Service("untyped-contract", "Untyped contract")
assert_type(UNTYPED, Service[BaseModel, BaseModel])
