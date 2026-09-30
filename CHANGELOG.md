# Changelog

Значимые пользовательские изменения Identity Hub фиксируются в этом файле.
Формат основан на Keep a Changelog; до первого стабильного релиза API может
меняться без периода совместимости.

## [Unreleased]

### Changed

- Extra `telegram` больше не тянет `aiogram`: уведомления отправляются прямым
  вызовом Bot API `sendMessage` через `httpx` (`HttpTelegramTransport`).
  `AiogramTelegramTransport` удалён; протокол `TelegramTransport` не изменился.
- Ответы Bot API 400/401/403/404 (`TelegramRejected`) сразу переводят уведомление
  в `dead` без исчерпания ретраев; сеть и 5xx (`TelegramUnavailable`) повторяются.

## [1.0.0rc1] - 2026-09-27

### Added

- Полное руководство по установке, быстрому старту, CLI и production-интеграции.
- Исполняемый Telegram-подобный пример на `MemoryStore`.
- Стабильная корневая поверхность импортов для сценария публичного API.
- Матрица трассировки всех требований FR/G/T/S на исполняемые тесты.

## [0.1.0.dev0] - 2026-09-27

### Added

- Декларативные сервисы, пространства, аккаунты, регионы состояний и связи.
- Конкурентно безопасные аренды с heartbeat, fencing, квотами и pacing.
- Зашифрованные credentials, сессии, прокси и управляемая ротация KEK.
- PostgreSQL-хранилище, встроенные миграции Alembic и отдельные схемы.
- Прокси, вход, challenge, верификации, пробы, импорт и полнотекстовый поиск.
- Outbox, dispatcher, вебхуки, Telegram-уведомления, maintenance и метрики.
- CLI `migrate`, `dispatcher`, `maintenance`, `doctor` и `rotate-key`.
- `MemoryStore`, pytest-плагин, фабрики и общий conformance-набор для адаптеров.
