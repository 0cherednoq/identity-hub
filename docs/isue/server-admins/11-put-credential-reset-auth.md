# У `put_credential()` нет `reset_auth`

## Претензия

Точечная запись новых credentials не может явно очистить старый окончательный отказ `auth`.

## Подтверждение

Претензия обоснована. `replace_credentials(..., reset_auth=True)` атомарно переводит
`session_invalid` в `no_session` или очищает `code` и `retryable` у `no_session`.
`put_credential()` выполняется транзакционно, но такого параметра и шага не имеет. При этом сама
запись credentials очищает собственные `last_rejected_at` и `reject_code`, а агрегатное состояние
`auth.retryable=False` остаётся и может блокировать автоматический повтор входа.
