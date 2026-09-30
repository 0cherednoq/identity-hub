# Аналитические read-модели

`SpaceHandle` предоставляет secret-free read-модели для графиков админки. Backend публикует их
под `/api/v1/spaces/{space_slug}/analytics/*`; доступ сначала ограничивается
`AdminPrincipal.allowed_spaces`, а SQL и store rows не выходят за границу библиотеки.

## Каталог и ограничения

`GET .../analytics/catalog` возвращает таймзону пространства, допустимые метрики, dimensions,
bucket sizes, формулы и серверные лимиты. Это источник опций для UI, а не список, который frontend
дублирует вручную.

- диапазон должен быть непустым, timezone-aware и не длиннее 366 дней;
- допустимы сохранённые buckets `1m`, `1h`, `1d`; 5- и 15-минутные точки UI складывает из `1m`;
- максимум 50 групп и 1000 точек во всём ответе (`groups × buckets`);
- неизвестная метрика, dimension или bucket отклоняется с `400 validation_failed`;
- дневная корзина начинается в локальную полночь IANA timezone пространства. Поэтому сутки на
  переходе DST могут длиться 23 или 25 часов;
- отсутствующая строка внутри известного ряда означает ноль. `coverage=partial` означает, что
  исторических snapshot-marker'ов недостаточно, и не должно изображаться как подтверждённый ноль.

Каждый исторический ответ содержит детерминированный `cache_tag`. HTTP API отправляет его как
сильный `ETag`, `Cache-Control: private, max-age=0, must-revalidate` и отвечает `304` на совпавший
`If-None-Match`. Ответ зависит от пространства и principal, поэтому общий публичный кеш запрещён.

## Временные ряды

`analytics_series(metric, start, end, bucket_size, group_by, dimensions)` читает только
`ih_stats_rollup`, заполняет пропуски нулями и возвращает для каждой группы:

- `points` текущего окна;
- `total` как сумму пересекающих окно сохранённых buckets;
- `previous_total` для непосредственно предшествующего окна той же длины.

Каталог включает поступление (`identities.added`, dimension `method=register|import`), auth/login,
challenge, verification/tier, standing, override/drift, cooling/quota, lifecycle, lease/operation,
proxy status и bounded telemetry. `service` хранится отдельной dimension ключа роллапа; UUID,
external reference и секреты dimensions не являются.

## Производные отчёты

| Read-модель | Определение |
|---|---|
| `analytics_funnel` | аккаунты, добавленные в выбранном окне → первый `authenticated` → достигнутые tiers; доля считается от added |
| `analytics_time_to` | секунды от первого source до первого target; linear median и p90; verification attempts берутся из успешного события |
| `analytics_cohorts` | недельная когорта по локальной дате регистрации; доля `standing=good` через 1/7/14/30 дней среди уже наблюдаемых аккаунтов |
| `analytics_ban_rate` | когда-либо забаненные / добавленные в когорту, разрез `service`, `batch`, `tag` или `proxy_pool` |
| `analytics_utilization` | сумма пересечения lease intervals с окном / (`eligible_current × seconds_in_window`); heatmap использует локальные weekday/hour |
| `analytics_proxy_quality` | ошибки операций с `blame=proxy` и переходы прокси в quarantined/dead/alive |

Funnel, time-to, cohorts и ban rate могут учитывать outcomes после конца окна добавления, но один
запрос всё равно ограничен 366 днями. Старый агрегатный `identity.imported`, не содержащий UUID
строк, входит в intake denominator, но не может быть связан с последующими индивидуальными
outcomes; такой исторический funnel следует интерпретировать как нижнюю границу прохождения.

Heatmap операций располагает итоговые lease counters в корзине начала аренды: журнал не хранит
timestamp каждой отдельной операции. `eligible_current` явно назван текущим знаменателем, поэтому
это не выдуманная историческая численность. Точная историческая state distribution показывается
отдельно по `ih_state_snapshots` и её coverage markers.

## Проверка и эксплуатация

Роллапы должны обслуживаться consumer-ом `stats-rollups`, а snapshots — hourly maintenance job.
Для восстановления диапазона используйте команды из [`ROLLUPS.md`](ROLLUPS.md) и
[`SNAPSHOTS.md`](SNAPSHOTS.md). Golden tests фиксируют формулы, previous period, DST, timezone,
нулевые buckets, ETag/304 и пределы response cardinality.

## Страница админки

Маршрут `/spaces/{spaceSlug}/analytics` загружается отдельным route chunk. Параметры `window`,
`bucket`, `dimension`, `service`, а для custom-окна ещё `start` и `end`, проходят строгий parser и
сохраняются в URL. Каталог кешируется на сессию, а независимые series и производные отчёты
запрашиваются параллельно с общим AbortSignal.

Страница группирует поступление, авторизацию, верификацию, standing, нагрузку, качество данных и
прокси. У каждого временного графика всегда есть раскрываемая HTML-таблица, точная формула,
timezone, coverage и сравнение с предыдущим окном. Данные можно выгрузить как CSV одного ряда или
JSON всей страницы, изображение — как SVG без секретных payload. Длинные legends переносятся и
закреплены отдельным Chromium visual baseline.
