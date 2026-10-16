# `active_credential()` теряет имя выбранной записи

## Претензия

`Lease.active_credential()` и `LoginContext.active_credential()` выбирают конкретную запись
credentials, но возвращают только расшифрованную модель. Адаптер не получает `(kind, name)` и
поэтому не может надёжно передать ту же пару в `reject_credential()` или
`LoginFailure(credential=...)`.

## Подтверждение

Претензия обоснована. В `lease.py` внутренняя выборка располагает `CredentialRow`, включая
`kind` и `name`, но публичный метод возвращает только результат `_open_stored_model()`. Контекст
входа делегирует этому методу без сохранения выбранного ключа.

Если адаптер сообщает окончательный отказ без `credential`, запись не получает
`last_rejected_at`. Следующий вход снова выбирает первую по имени запись. Для автоматической
приёмки число повторов ограничено политикой, но обычные вызовы `login()`/`login_many()` могут
повторять ту же заведомо нерабочую запись.

## Временный обход для `1.0.0a3` и более ранних версий

До установки версии с исправлением адаптер должен использовать только заранее известное имя записи и при
отказе явно передавать его:

```python
credentials = await ctx.credential("default", "known-name")
if credentials is not None:
    raise LoginFailure(
        "credentials_rejected",
        retryable=False,
        credential=("default", "known-name"),
    )
```

Для динамически именованных записей в `1.0.0a3` надёжного обхода через `LoginContext` нет.
В следующей версии `active_credential()` возвращает `CredentialCandidate`; используйте
`candidate.value` для входа и `candidate.key` для `LoginFailure(credential=...)`.
