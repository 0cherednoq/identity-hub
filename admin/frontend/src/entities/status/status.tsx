import { StatusBadge } from "@/shared/components/status/status-badge";
import type { StatusTone } from "@/shared/components/status/status-badge";

export interface StatusView {
  label: string;
  tone: StatusTone;
}

export type StatusRegistry<T extends string> = Record<T, StatusView>;

export const LIFECYCLE = ["provisioning", "ready", "retired"] as const;
export type Lifecycle = (typeof LIFECYCLE)[number];
export const LIFECYCLE_VIEW: StatusRegistry<Lifecycle> = {
  provisioning: { label: "Подготовка", tone: "info" },
  ready: { label: "Готов", tone: "success" },
  retired: { label: "Выведен", tone: "neutral" },
};

export const STANDING = [
  "good",
  "limited",
  "frozen",
  "appealing",
  "banned",
  "deleted",
] as const;
export type Standing = (typeof STANDING)[number];
export const STANDING_VIEW: StatusRegistry<Standing> = {
  good: { label: "В порядке", tone: "success" },
  limited: { label: "Ограничен", tone: "warning" },
  frozen: { label: "Заморожен", tone: "danger" },
  appealing: { label: "Апелляция", tone: "info" },
  banned: { label: "Забанен", tone: "danger" },
  deleted: { label: "Удалён", tone: "neutral" },
};

export const AUTH_STATE = [
  "no_session",
  "logging_in",
  "challenge",
  "authenticated",
  "session_invalid",
] as const;
export type AuthState = (typeof AUTH_STATE)[number];
export const AUTH_STATE_VIEW: StatusRegistry<AuthState> = {
  no_session: { label: "Нет сессии", tone: "neutral" },
  logging_in: { label: "Вход", tone: "info" },
  challenge: { label: "Нужен код", tone: "warning" },
  authenticated: { label: "Авторизован", tone: "success" },
  session_invalid: { label: "Сессия недействительна", tone: "danger" },
};

export const AVAILABILITY = [
  "available",
  "paused",
  "cooling",
  "quarantined",
] as const;
export type Availability = (typeof AVAILABILITY)[number];
export const AVAILABILITY_VIEW: StatusRegistry<Availability> = {
  available: { label: "Доступен", tone: "success" },
  paused: { label: "Приостановлен", tone: "neutral" },
  cooling: { label: "Остывает", tone: "warning" },
  quarantined: { label: "Карантин", tone: "danger" },
};

export const CHANGE_SOURCE = [
  "command",
  "probe",
  "error",
  "webhook",
  "override",
  "timer",
  "import",
] as const;
export type ChangeSource = (typeof CHANGE_SOURCE)[number];
export const CHANGE_SOURCE_VIEW: StatusRegistry<ChangeSource> = {
  command: { label: "Команда", tone: "info" },
  probe: { label: "Проверка", tone: "info" },
  error: { label: "Ошибка", tone: "danger" },
  webhook: { label: "Webhook", tone: "success" },
  override: { label: "Ручная правка", tone: "warning" },
  timer: { label: "Таймер", tone: "neutral" },
  import: { label: "Импорт", tone: "neutral" },
};

export const SESSION_STATUS = ["valid", "invalid", "expired"] as const;
export type SessionStatus = (typeof SESSION_STATUS)[number];
export const SESSION_STATUS_VIEW: StatusRegistry<SessionStatus> = {
  valid: { label: "Действует", tone: "success" },
  invalid: { label: "Недействительна", tone: "danger" },
  expired: { label: "Истекла", tone: "neutral" },
};

export const VERIFICATION_STATE = [
  "missing",
  "pending",
  "under_review",
  "verified",
  "rejected",
  "expired",
  "revoked",
] as const;
export type VerificationState = (typeof VERIFICATION_STATE)[number];
export const VERIFICATION_STATE_VIEW: StatusRegistry<VerificationState> = {
  missing: { label: "Нет данных", tone: "neutral" },
  pending: { label: "В процессе", tone: "info" },
  under_review: { label: "На проверке", tone: "warning" },
  verified: { label: "Подтверждено", tone: "success" },
  rejected: { label: "Отклонено", tone: "danger" },
  expired: { label: "Истекло", tone: "neutral" },
  revoked: { label: "Отозвано", tone: "danger" },
};

export const PROXY_STATUS = ["alive", "quarantined", "dead"] as const;
export type ProxyStatus = (typeof PROXY_STATUS)[number];
export const PROXY_STATUS_VIEW: StatusRegistry<ProxyStatus> = {
  alive: { label: "Работает", tone: "success" },
  quarantined: { label: "Карантин", tone: "warning" },
  dead: { label: "Недоступен", tone: "danger" },
};

export const LEASE_END_REASON = [
  "released",
  "failed",
  "lost",
  "expired",
  "revoked",
] as const;
export type LeaseEndReason = (typeof LEASE_END_REASON)[number];
export const LEASE_END_REASON_VIEW: StatusRegistry<LeaseEndReason> = {
  released: { label: "Освобождена", tone: "success" },
  failed: { label: "Ошибка", tone: "danger" },
  lost: { label: "Потеряна", tone: "danger" },
  expired: { label: "Истекла", tone: "neutral" },
  revoked: { label: "Отозвана", tone: "warning" },
};

export const SEVERITY = ["info", "warning", "critical"] as const;
export type Severity = (typeof SEVERITY)[number];
export const SEVERITY_VIEW: StatusRegistry<Severity> = {
  info: { label: "Информация", tone: "info" },
  warning: { label: "Предупреждение", tone: "warning" },
  critical: { label: "Критично", tone: "danger" },
};

export const DELIVERY_STATUS = [
  "pending",
  "succeeded",
  "failed",
  "dead",
] as const;
export type DeliveryStatus = (typeof DELIVERY_STATUS)[number];
export const DELIVERY_STATUS_VIEW: StatusRegistry<DeliveryStatus> = {
  pending: { label: "Ожидает", tone: "info" },
  succeeded: { label: "Доставлено", tone: "success" },
  failed: { label: "Повтор", tone: "warning" },
  dead: { label: "Исчерпано", tone: "danger" },
};

export const STATUS_GROUPS = [
  { label: "Lifecycle", values: LIFECYCLE, view: LIFECYCLE_VIEW },
  { label: "Standing", values: STANDING, view: STANDING_VIEW },
  { label: "AuthState", values: AUTH_STATE, view: AUTH_STATE_VIEW },
  { label: "Availability", values: AVAILABILITY, view: AVAILABILITY_VIEW },
  { label: "ChangeSource", values: CHANGE_SOURCE, view: CHANGE_SOURCE_VIEW },
  { label: "SessionStatus", values: SESSION_STATUS, view: SESSION_STATUS_VIEW },
  {
    label: "VerificationState",
    values: VERIFICATION_STATE,
    view: VERIFICATION_STATE_VIEW,
  },
  { label: "ProxyStatus", values: PROXY_STATUS, view: PROXY_STATUS_VIEW },
  {
    label: "LeaseEndReason",
    values: LEASE_END_REASON,
    view: LEASE_END_REASON_VIEW,
  },
  { label: "Severity", values: SEVERITY, view: SEVERITY_VIEW },
  {
    label: "DeliveryStatus",
    values: DELIVERY_STATUS,
    view: DELIVERY_STATUS_VIEW,
  },
] as const;

export function resolveStatus<T extends string>(
  registry: StatusRegistry<T>,
  value: string,
): StatusView & { unknown: boolean } {
  if (Object.hasOwn(registry, value)) {
    return { ...registry[value as T], unknown: false };
  }
  return {
    label: `Неизвестно: ${value || "(пусто)"}`,
    tone: "danger",
    unknown: true,
  };
}

export function DomainStatusBadge<T extends string>({
  registry,
  value,
}: {
  registry: StatusRegistry<T>;
  value: string;
}) {
  const status = resolveStatus(registry, value);
  return (
    <StatusBadge tone={status.tone} unknown={status.unknown}>
      {status.label}
    </StatusBadge>
  );
}
