# Фронтенд: правила для агента

## Всегда

- Перед вёрсткой открой каталог компонентов (`FRONTEND-DESIGN-SYSTEM.md` §3.3) и выбери компонент.
- Страница = шаблон (`ListPage` / `DetailPage` / `DashboardPage`) + слоты.
- Статусы — только `StatusBadge` с тоном из `entities/*/status.ts`.
- Цвета, радиусы, тени, размеры шрифта — только из токенов; в `pages/`, `features/`, `entities/` в `className` — только раскладка (`flex`, `grid`, `gap`, `col-span`, `items`, `justify`, `w-full`, `min-w-0`).
- Данные — только через хуки `entities/*/api.ts` поверх сгенерированного клиента `shared/api`.
- Каждый блок с данными — в `QueryState`.
- Новый визуальный вариант — именованный вариант `cva` в `shared/components`, плюс `/_design`.

## Никогда

- Классы стандартной палитры (`red-500`, `gray-200` и т.п.) — их нет в сборке.
- Произвольные значения в квадратных скобках: `w-[347px]`, `text-[13px]`, `bg-[#fff]`.
- `style={{…}}`, `dark:…`, `rounded-*`, `shadow-*`, `border-2`, `text-{размер}` вне `shared/`.
- Сырые `<button>`, `<input>`, `<select>`, `<table>`, `<textarea>` вне `shared/ui`.
- Прямой импорт `@radix-ui/*` вне `shared/ui`; `recharts` вне `shared/components/charts`.
- Копия компонента «с небольшой правкой».
- Правка файлов в `shared/ui` без записи в `shared/ui/CHANGES.md`.
- Ручная правка `shared/api` (генерируется из OpenAPI).

## Перед завершением

- `npm run check` (типы, ESLint, форматирование) — без ошибок.
- Если менялся `shared/components`: страница `/_design` обновлена, `npm run test:visual` пройден.
