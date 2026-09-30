import type {
  AnalyticsSearch,
  AnalyticsSearchPatch,
} from "@/shared/lib/analytics-search";
import { Input } from "@/shared/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";

function localInput(value: string | undefined): string {
  return value === undefined ? "" : value.slice(0, 16);
}

function isoInput(value: string): string | undefined {
  if (value === "") return undefined;
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : undefined;
}

export function AnalyticsControls({
  value,
  onPatch,
}: {
  value: AnalyticsSearch;
  onPatch: (patch: AnalyticsSearchPatch) => void;
}) {
  const window = value.window ?? "7d";
  return (
    <section className="bg-card grid gap-4 rounded-lg border p-4 md:grid-cols-2 xl:grid-cols-5">
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Окно</span>
        <Select
          value={window}
          onValueChange={(next) => {
            onPatch({ window: next, start: undefined, end: undefined });
          }}
        >
          <SelectTrigger className="w-full" aria-label="Окно аналитики">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="1d">24 часа</SelectItem>
            <SelectItem value="7d">7 дней</SelectItem>
            <SelectItem value="30d">30 дней</SelectItem>
            <SelectItem value="custom">Диапазон</SelectItem>
          </SelectContent>
        </Select>
      </label>
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Шаг</span>
        <Select
          value={value.bucket ?? (window === "1d" ? "1h" : "1d")}
          onValueChange={(bucket) => {
            onPatch({ bucket });
          }}
        >
          <SelectTrigger className="w-full" aria-label="Шаг аналитики">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="1m">1 минута</SelectItem>
            <SelectItem value="1h">1 час</SelectItem>
            <SelectItem value="1d">1 день</SelectItem>
          </SelectContent>
        </Select>
      </label>
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Разрез</span>
        <Select
          value={value.dimension ?? "service"}
          onValueChange={(dimension) => {
            onPatch({ dimension });
          }}
        >
          <SelectTrigger className="w-full" aria-label="Разрез аналитики">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">Без разреза</SelectItem>
            <SelectItem value="service">Сервис</SelectItem>
            <SelectItem value="method">Способ поступления</SelectItem>
            <SelectItem value="factor">Фактор</SelectItem>
            <SelectItem value="state">Состояние</SelectItem>
            <SelectItem value="code">Код</SelectItem>
            <SelectItem value="kind">Вид операции</SelectItem>
            <SelectItem value="pool">Пул</SelectItem>
            <SelectItem value="batch">Партия</SelectItem>
            <SelectItem value="tag">Тег</SelectItem>
            <SelectItem value="proxy_pool">Пул прокси</SelectItem>
          </SelectContent>
        </Select>
      </label>
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Сервис</span>
        <Input
          aria-label="Сервис аналитики"
          value={value.service ?? ""}
          placeholder="Все сервисы"
          maxLength={64}
          onChange={(event) => {
            onPatch({ service: event.target.value || undefined });
          }}
        />
      </label>
      <div className="text-muted-foreground flex items-end text-xs">
        Фильтры сохраняются в URL. Неподдерживаемый разрез конкретной метрики
        безопасно сворачивается в «все».
      </div>
      {window !== "custom" ? null : (
        <div className="grid gap-4 md:col-span-2 md:grid-cols-2 xl:col-span-5">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Начало</span>
            <Input
              aria-label="Начало диапазона"
              type="datetime-local"
              value={localInput(value.start)}
              onChange={(event) => {
                onPatch({ start: isoInput(event.target.value) });
              }}
            />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Конец</span>
            <Input
              aria-label="Конец диапазона"
              type="datetime-local"
              value={localInput(value.end)}
              onChange={(event) => {
                onPatch({ end: isoInput(event.target.value) });
              }}
            />
          </label>
        </div>
      )}
    </section>
  );
}
