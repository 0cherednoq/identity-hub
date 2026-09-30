import type { AdminErrorCode } from "./generated/types.gen";

export const ADMIN_ERROR_LABELS = {
  auth_required: "Требуется вход",
  access_denied: "Недостаточно прав",
  not_found: "Объект не найден",
  validation_failed: "Некорректные данные",
  conflict: "Конфликт изменений",
  revision_mismatch: "Данные уже изменились",
  idempotency_conflict: "Ключ запроса уже использован",
  rate_limited: "Слишком много запросов",
  dependency_unavailable: "Зависимость недоступна",
  internal_error: "Внутренняя ошибка",
} satisfies Record<AdminErrorCode, string>;
