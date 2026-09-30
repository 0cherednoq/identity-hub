# Миграция на неймспейсы пространства

Плоские методы `SpaceHandle` переезжают в ресурсные неймспейсы (`IMP-23`, решение `D-141`).
До `1.0.0b1` старые имена работают как алиасы и выдают `DeprecationWarning` с указанием
замены; в `1.0.0b1` они удаляются. `space.using`, `space.ref`, `space.webhooks` и
`space.notifications` не меняются.

Таблица строится из `identity_hub.api._migration.LEGACY`; тест
`tests/test_space_surface.py` проверяет, что они совпадают. Колонка «Готово» показывает,
с какой задачи доступно новое имя.

Термин: публичные имена используют `identity` (аккаунт во внешнем сервисе), не `account`.

| Было | Стало | Готово | Примечание |
|---|---|---|---|
| `space.access_card` | `space.secrets.describe` | I8.5 | Метаданные credentials и sessions без секретов |
| `space.account_search` | `space.admin.identity_page` | — | Read-модель админки |
| `space.account_selection_snapshot` | `space.admin.selection_snapshot` | — | Замороженный набор id+revision для bulk-действий админки |
| `space.add_proxies` | `space.proxies.create_many` | I8.6 | `pool=` → `group=` |
| `space.add_proxy` | `space.proxies.ensure` | I8.6 | Повтор с тем же адресом возвращает существующий прокси, как и раньше; `pool=` → `group=` |
| `space.add_requirement` | `space.verifications.requirements.add` | — |  |
| `space.add_tags` | `space.identities.add_tags` | I8.4 |  |
| `space.analytics_ban_rate` | `space.analytics.ban_rate` | — |  |
| `space.analytics_cohorts` | `space.analytics.cohorts` | — |  |
| `space.analytics_funnel` | `space.analytics.funnel` | — |  |
| `space.analytics_proxy_quality` | `space.analytics.proxy_quality` | — |  |
| `space.analytics_series` | `space.analytics.series` | — |  |
| `space.analytics_time_to` | `space.analytics.time_to` | — |  |
| `space.analytics_utilization` | `space.analytics.utilization` | — |  |
| `space.answer_challenge` | `space.auth.answer_challenge` | — |  |
| `space.cancel_import` | `space.imports.cancel` | I8.5 |  |
| `space.delete` | `space.identities.delete` | I8.4 |  |
| `space.delivery_center` | `space.admin.delivery_center` | — |  |
| `space.event_detail` | `space.admin.event_detail` | — |  |
| `space.event_page` | `space.admin.event_page` | — |  |
| `space.expire_challenges` | `space.maintenance.expire_challenges` | — |  |
| `space.expire_verifications` | `space.maintenance.expire_verifications` | — |  |
| `space.get_proxy` | `space.proxies.get` | I8.6 |  |
| `space.human_queue` | `space.admin.human_queue` | — |  |
| `space.identity_card` | `space.identities.card` | I8.2 | Состояние, квоты, аренда, blockers; лёгкий объект — `identities.get` |
| `space.impact_of` | `space.links.impact` | — |  |
| `space.import_files` | `space.imports.run_files` | I8.5 |  |
| `space.import_many` | `space.imports.run` | I8.5 | Параметры и `ImportReport` без изменений; для простого атомарного создания — `identities.create_many` |
| `space.import_run` | `space.imports.get` | I8.5 |  |
| `space.import_runs` | `space.admin.import_runs` | — |  |
| `space.lease_dashboard` | `space.admin.lease_dashboard` | — |  |
| `space.lease_history` | `space.admin.lease_history` | — |  |
| `space.lease_history_page` | `space.admin.lease_history_page` | — |  |
| `space.lease_stats` | `space.admin.lease_stats` | — |  |
| `space.link` | `space.links.add` | — |  |
| `space.link_summary` | `space.admin.link_summary` | — |  |
| `space.links_of` | `space.links.of` | — |  |
| `space.load_data` | `space.data.load` | — |  |
| `space.override` | `space.identities.override` | I8.4 |  |
| `space.override_proxy` | `space.proxies.override` | I8.6 |  |
| `space.pool` | `space.pools.define` | I8.6 | Объект правил отбора и аренды; в БД не хранится |
| `space.pool_capacity` | `space.admin.pool_capacity` | — |  |
| `space.pool_summary` | `space.admin.pool_summary` | — |  |
| `space.provider_operations` | `space.admin.provider_operations` | — |  |
| `space.proxy_detail` | `space.admin.proxy_detail` | — |  |
| `space.proxy_health` | `space.proxies.health` | I8.6 | Политика здоровья прокси хаба |
| `space.proxy_overview` | `space.admin.proxy_overview` | — |  |
| `space.proxy_url_of` | `space.proxies.url` | I8.6 | Теперь `await space.proxies.url(proxy_id)`; URL с паролем — не для логов |
| `space.rebind_proxy` | `space.proxies.rebind` | I8.6 |  |
| `space.recheck_proxies` | `space.proxies.recheck_due` | I8.6 | Прокси, у которых наступил срок перепроверки |
| `space.recheck_proxy` | `space.proxies.recheck` | I8.6 |  |
| `space.reconcile` | `space.sync.refresh_many` | — | Плановая сверка многих identity |
| `space.record_admin_bulk` | `space.admin.record_bulk` | — |  |
| `space.record_admin_export` | `space.admin.record_export` | — |  |
| `space.record_admin_import` | `space.admin.record_import` | — |  |
| `space.record_admin_integration` | `space.admin.record_integration` | — |  |
| `space.register` | `space.identities.ensure` | I8.3 | Семантика get-or-create сохранена; `identities.create` бросает `AlreadyExists` |
| `space.reindex_search` | `space.maintenance.reindex_search` | — |  |
| `space.remove_tags` | `space.identities.remove_tags` | I8.4 |  |
| `space.report_proxy_failure` | `space.proxies.report_failure` | I8.6 |  |
| `space.report_proxy_success` | `space.proxies.report_success` | I8.6 |  |
| `space.resolve_requirement` | `space.verifications.requirements.resolve` | — |  |
| `space.restore_standing` | `space.identities.restore_standing` | I8.4 |  |
| `space.resync` | `space.sync.refresh` | — | Одна проба одного identity |
| `space.retire` | `space.identities.retire` | I8.4 |  |
| `space.reveal_secret` | `space.secrets.reveal` | I8.5 | Поле задаётся `SecretRef`, строки временно принимаются |
| `space.review_verification` | `space.verifications.review` | — |  |
| `space.revoke_lease` | `space.pools.revoke` | I8.6 |  |
| `space.run_login` | `space.auth.login` | — | Возвращает `LoginOutcome` (`StrEnum`, значения прежние) |
| `space.run_logins` | `space.auth.login_many` | — |  |
| `space.save_data` | `space.data.save` | — |  |
| `space.search` | `space.identities.search` | I8.2 |  |
| `space.set_attributes` | `space.identities.replace_attributes` | I8.4 | Полная замена, как и раньше |
| `space.set_labels` | `space.identities.replace_labels` | I8.4 | Полная замена; для слияния — `identities.update(labels=...)` |
| `space.set_lifecycle` | `space.identities.set_lifecycle` | I8.4 |  |
| `space.set_note` | `space.identities.set_note` | I8.4 |  |
| `space.set_tags` | `space.identities.set_tags` | I8.4 |  |
| `space.space_overview` | `space.admin.overview` | — |  |
| `space.stage_import` | `space.imports.stage` | I8.5 |  |
| `space.unlink` | `space.links.remove` | — |  |
| `space.unpin` | `space.identities.unpin` | I8.4 |  |
| `space.update_credentials` | `space.secrets.replace_credentials` | I8.5 | Полная замена, как и раньше |
| `space.update_profile` | `space.identities.update` | I8.4 | Слияние; `replace=True` → `identities.replace_profile` |
| `space.update_session` | `space.secrets.replace_session` | I8.5 | Вне аренды; под арендой — `lease.save_session` |
| `space.verification_status` | `space.verifications.status` | — |  |

## Новое без старого аналога

| Метод | Готово | Что делает |
|---|---|---|
| `space.identities.get(id)` | I8.2 | `Identity` без секретов или `NotFound` |
| `space.identities.find(id)` / `find(service=, external_ref=)` | I8.2 | `Identity` или `None` |
| `space.identities.get_by_ref(service, ref)` | I8.2 | `Identity` или `NotFound` |
| `space.identities.list(IdentityFilter(...))` | I8.2 | страница по структурированному фильтру |
| `space.identities.count(IdentityFilter(...))` | I8.2 | число identity по фильтру |
| `space.identities.create(IdentityCreate(...))` | I8.3 | создаёт; существующая — `AlreadyExists` |
| `space.identities.ensure(IdentityCreate(...))` | I8.3 | `(Identity, created)`; существующая не меняется |
| `space.identities.create_many([...], on_conflict=)` | I8.3 | атомарно до 1000 штук, `fail` или `skip` |
| `space.identities.activate(id, actor=)` | I8.4 | перевести в работу (`lifecycle → ready`), пара к `retire` |
| `space.identities.update(id, labels=, note=)` | I8.4 | слияние меток и замена заметки одной транзакцией и одним событием |
| `space.identities.replace_profile(id, ...)` | I8.4 | полная замена профиля (бывш. `update_profile(replace=True)`) |
| `SecretRef.CREDENTIALS / SecretRef.session(slot) / SecretRef.PROFILE / SecretRef.evidence(factor)` | I8.5 | типизированная ссылка на секрет для `secrets.reveal` |
| `space.proxies.create(url, group=)` | I8.6 | добавить; тот же адрес — `AlreadyExists` |
| `space.proxies.list(group=, status=)` | I8.6 | страница прокси с `bound_count` |
| `pool.capacity()` | I8.6 | бывш. `pool.check_capacity()` |
| `ProxyPolicy(group=...)` | I8.6 | бывш. `ProxyPolicy(pool=...)` |
