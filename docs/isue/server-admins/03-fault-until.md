# У `Fault` нет срока (`until`)

**Библиотека:** `identity_hub==1.0.0a2`. Пути — от `site-packages/identity_hub/`. Найдено чтением кода и тестами server-admins (хаб на `MemoryStore` и на PostgreSQL).

**Тип:** недостающий функционал. **Важность:** средняя.

`Fault` (`protocols.py`, стр. 220–228) не принимает срок. Ветка `IDENTITY_BANNED` в
`operation.py::_on_failure` строит `Observation` без срока, поэтому временное ограничение
платформы («снимем через 24 часа») из операции не записать.

**Обход в server-admins:** после операции репортёр досылает наблюдение того же состояния со
сроком; библиотека отвечает `UNCHANGED` и записывает `until`
(`domain/regions.py::_apply_observation`, `until=obs.until or cur.until`). Это лишний вызов и
зависимость от деталей реализации.

**Предложение:** поле `until: datetime | None` (или `retry_after`) у `Fault`, которое попадает в
наблюдение.
