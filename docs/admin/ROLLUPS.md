# Временные роллапы аналитики

`ih_stats_rollup` — долговечный источник временных рядов админки. Запросы графиков за 30 дней
читают эту таблицу и не сканируют `ih_events`.

## Контракт хранения

Уникальный ключ строки: `space_id + service + metric + dims + bucket_start + bucket_size`.
Поддерживаются корзины `1m`, `1h` и `1d`. Минутные и часовые границы считаются в UTC. Дневная
граница — локальная полночь IANA-таймзоны пространства и поэтому корректно переживает дни длиной
23 или 25 часов. Отсутствующая строка при чтении означает ноль, а не «нет данных».

Consumer `stats-rollups` читает и внешние, и внутренние journal events по существующему
`(tx_id, id)`-курсору. Для затронутых корзин он заново вычисляет абсолютное значение из сырого
журнала и делает UPSERT. Поэтому повтор одной пачки не увеличивает итог, а транзакция с ранним ID,
закоммиченная позднее, не теряется.

Синхронный MetricsHook не пишет в БД на critical path. Встроенный буфер складывает ограниченные
labels в памяти и сбрасывается dispatcher-ом. Каждая пачка получает UUID; запись receipt в
`ih_stats_metric_batches` и изменение роллапов выполняются одной транзакцией. Неопределённый
результат можно повторить без двойного счёта. Потеря процесса до первого flush может потерять не
более текущей минутной пачки оперативной телеметрии; доменные event-rollups от этого не зависят.

## Метрики событий

- `events.total` с dimension `type`;
- `identities.added`, `auth.authenticated`, `auth.session_lost`;
- `logins.succeeded`, `logins.failed`;
- `verifications.verified`, `verifications.rejected`;
- `tiers.reached`, `tiers.lost`;
- `standings.entered`, `standings.recovered`, `state.overrides`, `lifecycle.retired`;
- `leases.acquired`, `leases.ended`, `operations.failed`.

Метрики hook сохраняются с префиксом `telemetry.`. Histogram представлен рядами `.sum` и
`.count`; среднее вычисляется как `sum / count`. UUID аккаунта, аренды, события, импорта и
`external_ref` не допускаются в dimensions.

## Перестроение и retention

Перестроить диапазон из журнала событий:

```console
identity-hub rollups --factory app:hub --rebuild --space main \
  --start 2026-09-01T00:00:00Z --end 2026-10-01T00:00:00Z
```

Границы автоматически расширяются до целых затронутых корзин. Команда сначала удаляет эти
корзины, затем записывает raw-event oracle.

Минутные строки автоматически удаляются dispatcher-ом раз в час после 48 часов (срок меняется
через `Retention.rollup_minutes`). Ручная очистка:

```console
identity-hub rollups --factory app:hub --purge-minutes
```

Часовые и дневные строки этой командой не удаляются.
