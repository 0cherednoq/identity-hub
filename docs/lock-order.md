# Порядок блокировок

Требование `IMP-14`, реализация — задача `I0.2b`. Все транзакции библиотеки берут блокировки
строк PostgreSQL в одном порядке. Тогда параллельные операции ждут друг друга, а не попадают
в deadlock.

## Порядок

1. `ih_identities`. Если строк несколько — по возрастанию `id` через `StoreTx.lock_identities()`.
2. Связанный аккаунт из `ih_identity_links`: зависимый раньше связанного.
3. `ih_region_states`, `ih_verifications`, `ih_credentials`, `ih_sessions`.
4. `ih_proxies`, затем `ih_bindings`.
5. `ih_leases`, `ih_lease_history`.
6. `ih_provider_operations`.
7. `ih_events`, `ih_webhook_deliveries`, `ih_notifications`.

Диспетчер и обслуживание блокируют только строки своих очередей (`SKIP LOCKED`) и никогда не
блокируют аккаунт на время доставки или сетевого вызова.

## Правила для кода

- **Несколько identity в одной транзакции** — сначала `await tx.lock_identities(ids)`: одна
  блокировка `FOR NO KEY UPDATE` по возрастанию `id`, затем работа в любом порядке. `MemoryStore`
  сериализует транзакции (`D-124`), для него метод пустой.
- **Нельзя повышать `FOR SHARE` до записи**, если параллельная транзакция может держать `FOR SHARE`
  той же строки. Две транзакции с общей разделяемой блокировкой взаимно ждут повышения до
  `UPDATE`. Так падал импорт с `on_existing="update_session"` до I0.2b; его воспроизводит
  `tests/pg/test_lock_order.py::test_reverse_order_no_deadlock`.
- **Вставки с уникальным ключом** в пачке идут в детерминированном порядке:
  `identities.create_many` сортирует по `(service, external_ref)`, см. `D-143`.
- **Сетевой I/O не выполняется под блокировкой** (правило 0.3 плана улучшений).

## Где берутся блокировки

| Операция | Что блокируется | Порядок |
|---|---|---|
| Выбор кандидата аренды | строка `ih_identities`, `SKIP LOCKED` | одна строка |
| Захват аренды | `ih_leases` выбранного аккаунта | после identity |
| Импорт `update_session` | обновляемые identity пачки | `lock_identities`, по `id` |
| `create_many` | уникальные индексы новых строк | по `(service, external_ref)` |
| Команды админки с `expected_revision` | `ih_identities` (`lock_identity_revision`) | одна строка |
| Перепривязка прокси | identity, затем прокси | п. 1 → п. 4 |
| Диспетчер, вебхуки, уведомления | строки очередей, `SKIP LOCKED` | без аккаунтов |
| Истечение аренд | `ih_leases`, `SKIP LOCKED` | без аккаунтов |
