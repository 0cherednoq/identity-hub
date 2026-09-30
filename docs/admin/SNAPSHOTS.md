# Почасовые снимки состояния

`ih_state_snapshots` хранит распределение текущих region states по
`space + service + region + state` на начало часа UTC. Для verification region ключ фактора
включается в dimension как `verification:<factor>`.

## Coverage

Каждому снимку соответствует строка `ih_state_snapshot_coverage`, даже если пространство пустое.
Публичный `hub.state_snapshots.frames(...)` поэтому различает:

- frame есть, `complete=true`, но нужной строки нет — подтверждённый ноль;
- frame отсутствует — исторических данных за час ещё нет;
- `complete=false` — зарезервировано для явно неполного импорта исторических данных.

Текущий срез никогда не копируется назад во времени. `backfill_current()` создаёт только frame
текущего часа с `source=backfill`.

## Запись и конкуренция

Dispatcher вызывает snapshot job не чаще одного раза за UTC-час. PostgreSQL сначала создаёт
coverage marker по уникальному ключу `(space_id, taken_at)`, блокирует markers всех пространств в
стабильном порядке, затем заменяет counts в одной транзакции. Два workers могут выполнить один час,
но не создают дубли; последний завершившийся полный срез становится значением часа.

Снять текущее состояние вручную:

```console
identity-hub snapshots --factory app:hub --backfill-current
```

## Retention

По умолчанию frames хранятся 365 дней (`Retention.state_snapshots`). Удаление coverage marker
каскадно удаляет counts. Автоматическая очистка выполняется вместе с почасовой job; ручная:

```console
identity-hub snapshots --factory app:hub --purge
```
