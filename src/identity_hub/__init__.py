"""Публичный API Identity Hub."""

from identity_hub.api.identities import (
    CreateManyResult,
    Identity,
    IdentityCreate,
    IdentityFilter,
    IdentityRef,
    OnConflict,
    Page,
)
from identity_hub.api.proxies import Proxy, ProxyCreateManyResult
from identity_hub.api.secrets import SecretRef
from identity_hub.clock import Clock, SystemClock
from identity_hub.context import LeaseContext, current_lease
from identity_hub.crypto import EnvSecretsProvider, FileSecretsProvider, StaticSecretsProvider, generate_key
from identity_hub.dispatch.notifications import (
    TelegramRateLimited,
    TelegramRejected,
    TelegramTransport,
    TelegramUnavailable,
)
from identity_hub.dispatch.webhooks import WebhookPolicy
from identity_hub.domain.verification import VerificationMode, VerificationSpec, VerificationTier
from identity_hub.enums import (
    AuthState,
    Availability,
    ChangeSource,
    DeliveryStatus,
    LeaseEndReason,
    Lifecycle,
    ProxyStatus,
    SessionStatus,
    Severity,
    Standing,
    VerificationState,
)
from identity_hub.errors import (
    AlreadyExists,
    AttemptsExhausted,
    Blocker,
    ChallengeAlreadyOpen,
    ConcurrentModification,
    IdentityHubError,
    LeaseLost,
    LeaseUnavailable,
    NotFound,
    OperationNotAllowed,
    QuotaExhausted,
    SpaceMismatch,
    StaleFencing,
    TransitionNotAllowed,
    ValidationFailed,
    VerificationRequirementsNotMet,
)
from identity_hub.events import Event
from identity_hub.hub import IdentityHub, Retention, SpaceHandle
from identity_hub.importing import ImportRecord, ImportReport, OnExisting
from identity_hub.lease import Lease
from identity_hub.logging import LeaseContextFilter, LeaseLogPolicy, structlog_processor
from identity_hub.maintenance import MaintenanceSchedule
from identity_hub.metrics import Metric, MetricsHook, NullMetrics, RecordingMetrics
from identity_hub.operation import Operation
from identity_hub.pool import LeaseTiming, Pool
from identity_hub.protocols import (
    Authenticator,
    Blame,
    ErrorClassifier,
    EventConsumer,
    Fault,
    FileImporter,
    LinkedIdentity,
    LoginChallenge,
    LoginContext,
    ProbeContext,
    ProbeResult,
    Probes,
    SecretsProvider,
)
from identity_hub.proxies import ProxyHealthPolicy
from identity_hub.service import (
    LeaseMode,
    OperationRule,
    Pacing,
    ProxyDownPolicy,
    ProxyPolicy,
    Quota,
    Service,
)
from identity_hub.state import Override
from identity_hub.verifications import Verification

__version__ = "1.0.0a1"

__all__ = [
    "AlreadyExists",
    "AttemptsExhausted",
    "AuthState",
    "Authenticator",
    "Availability",
    "Blame",
    "Blocker",
    "ChallengeAlreadyOpen",
    "ChangeSource",
    "Clock",
    "ConcurrentModification",
    "CreateManyResult",
    "DeliveryStatus",
    "EnvSecretsProvider",
    "ErrorClassifier",
    "Event",
    "EventConsumer",
    "Fault",
    "FileImporter",
    "FileSecretsProvider",
    "Identity",
    "IdentityCreate",
    "IdentityFilter",
    "IdentityHub",
    "IdentityHubError",
    "IdentityRef",
    "ImportRecord",
    "ImportReport",
    "Lease",
    "LeaseContext",
    "LeaseContextFilter",
    "LeaseEndReason",
    "LeaseLogPolicy",
    "LeaseLost",
    "LeaseMode",
    "LeaseTiming",
    "LeaseUnavailable",
    "Lifecycle",
    "LinkedIdentity",
    "LoginChallenge",
    "LoginContext",
    "MaintenanceSchedule",
    "Metric",
    "MetricsHook",
    "NotFound",
    "NullMetrics",
    "OnConflict",
    "OnExisting",
    "Operation",
    "OperationNotAllowed",
    "OperationRule",
    "Override",
    "Pacing",
    "Page",
    "Pool",
    "ProbeContext",
    "ProbeResult",
    "Probes",
    "Proxy",
    "ProxyCreateManyResult",
    "ProxyDownPolicy",
    "ProxyHealthPolicy",
    "ProxyPolicy",
    "ProxyStatus",
    "Quota",
    "QuotaExhausted",
    "RecordingMetrics",
    "Retention",
    "SecretRef",
    "SecretsProvider",
    "Service",
    "SessionStatus",
    "Severity",
    "SpaceHandle",
    "SpaceMismatch",
    "StaleFencing",
    "Standing",
    "StaticSecretsProvider",
    "SystemClock",
    "TelegramRateLimited",
    "TelegramRejected",
    "TelegramTransport",
    "TelegramUnavailable",
    "TransitionNotAllowed",
    "ValidationFailed",
    "Verification",
    "VerificationMode",
    "VerificationRequirementsNotMet",
    "VerificationSpec",
    "VerificationState",
    "VerificationTier",
    "WebhookPolicy",
    "__version__",
    "current_lease",
    "generate_key",
    "structlog_processor",
]
