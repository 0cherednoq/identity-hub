"""Guarded lifecycle, region override and requirement commands."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, cast
from uuid import UUID

from identity_hub.state import Override
from litestar import Request, post
from litestar.datastructures import State
from litestar.params import FromPath
from pydantic import Field, SecretStr, model_validator

from identity_hub_admin.api.contracts import (
    Confirmation,
    ConfirmedMutationContext,
    ContractModel,
    MutationContext,
    ResourceEnvelope,
)
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.commands import CommandGateway, CommandScope


class LifecycleCommand(MutationContext):
    target: Literal["provisioning", "ready", "retired"]
    confirmation: Confirmation | None = None

    @model_validator(mode="after")
    def validate_retirement_confirmation(self) -> LifecycleCommand:
        if self.target == "retired" and self.confirmation is None:
            raise ValueError("retiring an account requires explicit confirmation")
        return self


class RestoreStandingCommand(ConfirmedMutationContext):
    pass


class OverrideStateCommand(ConfirmedMutationContext):
    region: Literal["lifecycle", "standing", "auth", "availability"]
    key: str = Field(default="", max_length=128)
    target: str = Field(min_length=1, max_length=64)
    pin_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_pin(self) -> OverrideStateCommand:
        if self.pin_seconds is not None and self.region in {"lifecycle", "availability"}:
            raise ValueError("column regions cannot be pinned")
        return self


class UnpinStateCommand(ConfirmedMutationContext):
    region: Literal["standing", "auth"]
    key: str = Field(default="", max_length=128)


class ResolveRequirementCommand(MutationContext):
    code: str = Field(min_length=1, max_length=128)


class RunLoginCommand(MutationContext):
    pass


class ResyncAccountCommand(MutationContext):
    regions: list[str] = Field(min_length=1, max_length=20)


class AnswerChallengeCommand(MutationContext):
    challenge_id: UUID
    answer: SecretStr = Field(min_length=1, max_length=4096)


class ReviewVerificationCommand(ConfirmedMutationContext):
    factor: str = Field(min_length=1, max_length=128)
    action: Literal["approve", "reject", "abandon"]
    code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_reject_code(self) -> ReviewVerificationCommand:
        if self.action == "reject" and not self.code:
            raise ValueError("verification rejection requires a code")
        return self


class AccountCommandResponse(ContractModel):
    action: Literal[
        "lifecycle",
        "restore",
        "override",
        "unpin",
        "resolve_requirement",
        "run_login",
        "resync",
        "answer_challenge",
        "verification_review",
    ]
    changed: bool
    outcome: str
    region: str | None = None
    key: str = ""
    state: str | None = None


def _gateway(state: State) -> CommandGateway:
    return cast(CommandGateway, state.command_gateway)


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/lifecycle",
    operation_id="admin_v1_account_lifecycle_post",
    tags=["account-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def lifecycle_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: LifecycleCommand,
) -> ResourceEnvelope[AccountCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[AccountCommandResponse]:
        decision = await scope.handle.identities.set_lifecycle(
            identity_id, data.target, actor=scope.actor, reason=scope.reason
        )
        return ResourceEnvelope(
            data=AccountCommandResponse(
                action="lifecycle",
                changed=decision.changed,
                outcome=decision.outcome,
                region="lifecycle",
                state=decision.state.state,
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.lifecycle.set",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"target": data.target},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/standing-restore",
    operation_id="admin_v1_account_standing_restore_post",
    tags=["account-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def restore_standing_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: RestoreStandingCommand,
) -> ResourceEnvelope[AccountCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[AccountCommandResponse]:
        decision = await scope.handle.identities.restore_standing(
            identity_id, actor=scope.actor, reason=scope.reason
        )
        return ResourceEnvelope(
            data=AccountCommandResponse(
                action="restore",
                changed=decision.changed,
                outcome=decision.outcome,
                region="standing",
                state=decision.state.state,
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.standing.restore",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"confirmation": data.confirmation.model_dump(mode="json")},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/state-override",
    operation_id="admin_v1_account_state_override_post",
    tags=["account-commands"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def override_state_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: OverrideStateCommand,
) -> ResourceEnvelope[AccountCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[AccountCommandResponse]:
        decision = await scope.handle.identities.override(
            identity_id,
            Override(
                region=data.region,
                key=data.key,
                state=data.target,
                reason=scope.reason,
                actor=scope.actor,
                pin_for=(timedelta(seconds=data.pin_seconds) if data.pin_seconds is not None else None),
                code=data.code,
            ),
        )
        return ResourceEnvelope(
            data=AccountCommandResponse(
                action="override",
                changed=decision.changed,
                outcome=decision.outcome,
                region=data.region,
                key=data.key,
                state=decision.state.state,
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.state.override",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload=data.model_dump(mode="json", exclude={"reason", "expected_revision", "dry_run"}),
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/state-unpin",
    operation_id="admin_v1_account_state_unpin_post",
    tags=["account-commands"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def unpin_state_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: UnpinStateCommand,
) -> ResourceEnvelope[AccountCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[AccountCommandResponse]:
        decision = await scope.handle.identities.unpin(
            identity_id, data.region, data.key, actor=scope.actor, reason=scope.reason
        )
        return ResourceEnvelope(
            data=AccountCommandResponse(
                action="unpin",
                changed=decision.changed,
                outcome=decision.outcome,
                region=data.region,
                key=data.key,
                state=decision.state.state,
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.state.unpin",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"region": data.region, "key": data.key},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/requirement-resolve",
    operation_id="admin_v1_account_requirement_resolve_post",
    tags=["account-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def resolve_requirement_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: ResolveRequirementCommand,
) -> ResourceEnvelope[AccountCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[AccountCommandResponse]:
        changed = await scope.handle.verifications.requirements.resolve(
            identity_id, data.code, actor=scope.actor, reason=scope.reason
        )
        return ResourceEnvelope(
            data=AccountCommandResponse(
                action="resolve_requirement",
                changed=changed,
                outcome="applied" if changed else "unchanged",
                key=data.code,
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.requirement.resolve",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"code": data.code},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/login",
    operation_id="admin_v1_account_login_post",
    tags=["account-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def run_login_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: RunLoginCommand,
) -> ResourceEnvelope[AccountCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[AccountCommandResponse]:
        outcome = await scope.handle.auth.login(identity_id)
        return ResourceEnvelope(
            data=AccountCommandResponse(action="run_login", changed=True, outcome=outcome, region="auth")
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.login.run",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        transaction_mode="external",
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/resync",
    operation_id="admin_v1_account_resync_post",
    tags=["account-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def resync_account_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: ResyncAccountCommand,
) -> ResourceEnvelope[AccountCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[AccountCommandResponse]:
        outcomes = await scope.handle.sync.refresh(identity_id, data.regions)
        return ResourceEnvelope(
            data=AccountCommandResponse(
                action="resync",
                changed=True,
                outcome=", ".join(f"{key}={value}" for key, value in sorted(outcomes.items())),
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.resync.run",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"regions": data.regions},
        transaction_mode="external",
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/challenge-answer",
    operation_id="admin_v1_account_challenge_answer_post",
    tags=["account-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def answer_challenge_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: AnswerChallengeCommand,
) -> ResourceEnvelope[AccountCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[AccountCommandResponse]:
        outcome = await scope.handle.auth.answer_challenge(
            data.challenge_id,
            data.answer.get_secret_value(),
            actor=scope.actor,
            expected_identity_id=identity_id,
        )
        return ResourceEnvelope(
            data=AccountCommandResponse(
                action="answer_challenge", changed=True, outcome=outcome, region="auth"
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.challenge.answer",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"challenge_id": str(data.challenge_id)},
        transaction_mode="external",
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/verification-review",
    operation_id="admin_v1_account_verification_review_post",
    tags=["account-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def review_verification_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: ReviewVerificationCommand,
) -> ResourceEnvelope[AccountCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[AccountCommandResponse]:
        status = await scope.handle.verifications.review(
            identity_id,
            data.factor,
            data.action,
            actor=scope.actor,
            reason=scope.reason,
            code=data.code,
        )
        return ResourceEnvelope(
            data=AccountCommandResponse(
                action="verification_review",
                changed=True,
                outcome="applied",
                region="verification",
                key=data.factor,
                state=status.state,
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation=f"identity.verification.{data.action}",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"factor": data.factor, "action": data.action, "code": data.code},
    )


ACCOUNT_COMMAND_ROUTE_HANDLERS = [
    lifecycle_command,
    restore_standing_command,
    override_state_command,
    unpin_state_command,
    resolve_requirement_command,
    run_login_command,
    resync_account_command,
    answer_challenge_command,
    review_verification_command,
]
