# `cool_down` из `quarantined` бросает `TransitionNotAllowed`

**Библиотека:** `identity_hub==1.0.0a2`. Пути — от `site-packages/identity_hub/`. Найдено чтением кода и тестами server-admins (хаб на `MemoryStore` и на PostgreSQL).

**Тип:** поведение, документация. **Важность:** низкая.

Переход `quarantined → cooling` не объявлен (`domain/region_specs.py`, стр. 97–101), поэтому
сигнал «подождите» для аккаунта в карантине бросает `TransitionNotAllowed`. Неясно, задумано ли
это: карантин строже охлаждения, но платформа может прислать «retry after» и такому аккаунту.

**Обход в server-admins:** репортёр возвращает `skipped`.

**Предложение:** возвращать `UNCHANGED` вместо исключения (карантин как более строгое состояние
сохраняется) или описать в документации, что `cool_down` к `quarantined` не применяется.
