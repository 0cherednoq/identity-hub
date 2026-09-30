import { useState, type SyntheticEvent } from "react";

import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";

export interface AccountFilterValue {
  attention?: "false" | "true" | undefined;
  auth?: string | undefined;
  availability?: string | undefined;
  createdFrom?: string | undefined;
  createdTo?: string | undefined;
  eligible?: "false" | "true" | undefined;
  factor?: string | undefined;
  factorState?: string | undefined;
  hasOverride?: "false" | "true" | undefined;
  lastLeasedFrom?: string | undefined;
  lastLeasedTo?: string | undefined;
  label?: string | undefined;
  leased?: "false" | "true" | undefined;
  lifecycle?: string | undefined;
  linked?: "false" | "true" | undefined;
  proxy?: string | undefined;
  quotaKind?: string | undefined;
  q?: string | undefined;
  requirement?: string | undefined;
  service?: string | undefined;
  standing?: string | undefined;
  standingChangedFrom?: string | undefined;
  standingChangedTo?: string | undefined;
  sort?: string | undefined;
  direction?: "asc" | "desc" | undefined;
  syncedFrom?: string | undefined;
  syncedTo?: string | undefined;
  tag?: string | undefined;
  tier?: string | undefined;
}

interface Option {
  label: string;
  value: string;
}

const LIFECYCLE: Option[] = [
  { label: "Подготовка", value: "provisioning" },
  { label: "Готов", value: "ready" },
  { label: "Выведен", value: "retired" },
];
const STANDING: Option[] = [
  "good",
  "limited",
  "frozen",
  "appealing",
  "banned",
  "deleted",
].map((value) => ({ label: value, value }));
const AUTH: Option[] = [
  "no_session",
  "logging_in",
  "challenge",
  "authenticated",
  "session_invalid",
].map((value) => ({ label: value, value }));
const AVAILABILITY: Option[] = [
  "available",
  "paused",
  "cooling",
  "quarantined",
].map((value) => ({ label: value, value }));
const BOOLEAN: Option[] = [
  { label: "Да", value: "true" },
  { label: "Нет", value: "false" },
];
const SORT_FIELDS: Option[] = [
  { label: "Добавлен", value: "created_at" },
  { label: "Сервис", value: "service" },
  { label: "External ref", value: "external_ref" },
  { label: "Lifecycle", value: "lifecycle" },
  { label: "Standing", value: "standing" },
  { label: "Auth", value: "auth" },
  { label: "Availability", value: "availability" },
  { label: "Пригодность", value: "eligible" },
  { label: "Уровень верификации", value: "verification_tier" },
  { label: "Аренда", value: "leased" },
  { label: "Последняя аренда", value: "last_leased_at" },
  { label: "Последняя сверка", value: "synced_at" },
  { label: "Error score", value: "error_score" },
  { label: "Профиль: страна", value: "profile:country" },
];
const SORT_DIRECTIONS: Option[] = [
  { label: "По убыванию", value: "desc" },
  { label: "По возрастанию", value: "asc" },
];

function FilterSelect({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: Option[];
  value?: string | undefined;
  onChange: (value: string | undefined) => void;
}) {
  return (
    <Select
      value={value ?? "all"}
      onValueChange={(next) => {
        onChange(next === "all" ? undefined : next);
      }}
    >
      <SelectTrigger aria-label={label}>
        <SelectValue placeholder={label} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="all">{label}: все</SelectItem>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function TextFilter({
  label,
  value,
  onChange,
  type = "text",
}: {
  label: string;
  value?: string | undefined;
  onChange: (value: string | undefined) => void;
  type?: "datetime-local" | "text";
}) {
  return (
    <label className="grid gap-1 text-sm font-medium">
      {label}
      <Input
        type={type}
        value={value ?? ""}
        onChange={(event) => {
          onChange(event.target.value || undefined);
        }}
      />
    </label>
  );
}

function AccountSearchForm({
  initialQuery,
  onQueryChange,
  onReset,
}: {
  initialQuery: string;
  onQueryChange: (query: string | undefined) => void;
  onReset: () => void;
}) {
  const [draft, setDraft] = useState(initialQuery);
  const submit = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    onQueryChange(draft || undefined);
  };

  return (
    <form className="flex flex-col gap-2 sm:flex-row" onSubmit={submit}>
      <Input
        aria-label="Поиск аккаунтов"
        placeholder="service:telegram standing:banned country:UA olena"
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
        }}
      />
      <Button type="submit">Найти</Button>
      <Button type="button" variant="outline" onClick={onReset}>
        Сбросить
      </Button>
    </form>
  );
}

export function AccountFilters({
  value,
  services,
  onPatch,
  onReset,
}: {
  value: AccountFilterValue;
  services: string[];
  onPatch: (patch: Partial<AccountFilterValue>) => void;
  onReset: () => void;
}) {
  const patch = (key: keyof AccountFilterValue, next: string | undefined) => {
    onPatch({ [key]: next });
  };

  return (
    <div className="grid gap-4">
      <AccountSearchForm
        initialQuery={value.q ?? ""}
        key={value.q ?? ""}
        onQueryChange={(query) => {
          patch("q", query);
        }}
        onReset={onReset}
      />
      <div className="flex flex-wrap gap-2" aria-label="Быстрые пресеты">
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            onPatch({ attention: "true" });
          }}
        >
          Требуют внимания
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            onPatch({ eligible: "true" });
          }}
        >
          Пригодные
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            onPatch({ leased: "true" });
          }}
        >
          В аренде
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            onPatch({ hasOverride: "true" });
          }}
        >
          С override
        </Button>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <FilterSelect
          label="Сортировка"
          options={SORT_FIELDS}
          value={value.sort ?? "created_at"}
          onChange={(next) => {
            patch("sort", next === "created_at" ? undefined : next);
          }}
        />
        <FilterSelect
          label="Направление"
          options={SORT_DIRECTIONS}
          value={value.direction ?? "desc"}
          onChange={(next) => {
            patch("direction", next === "desc" ? undefined : next);
          }}
        />
      </div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <FilterSelect
          label="Сервис"
          options={services.map((service) => ({
            label: service,
            value: service,
          }))}
          value={value.service}
          onChange={(next) => {
            patch("service", next);
          }}
        />
        <FilterSelect
          label="Lifecycle"
          options={LIFECYCLE}
          value={value.lifecycle}
          onChange={(next) => {
            patch("lifecycle", next);
          }}
        />
        <FilterSelect
          label="Standing"
          options={STANDING}
          value={value.standing}
          onChange={(next) => {
            patch("standing", next);
          }}
        />
        <FilterSelect
          label="Auth"
          options={AUTH}
          value={value.auth}
          onChange={(next) => {
            patch("auth", next);
          }}
        />
        <FilterSelect
          label="Availability"
          options={AVAILABILITY}
          value={value.availability}
          onChange={(next) => {
            patch("availability", next);
          }}
        />
        <FilterSelect
          label="Пригоден"
          options={BOOLEAN}
          value={value.eligible}
          onChange={(next) => {
            patch("eligible", next);
          }}
        />
        <FilterSelect
          label="В аренде"
          options={BOOLEAN}
          value={value.leased}
          onChange={(next) => {
            patch("leased", next);
          }}
        />
        <FilterSelect
          label="Есть override"
          options={BOOLEAN}
          value={value.hasOverride}
          onChange={(next) => {
            patch("hasOverride", next);
          }}
        />
      </div>
      <details className="rounded-md border p-3">
        <summary className="cursor-pointer text-sm font-medium">
          Расширенные фильтры
        </summary>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {(
            [
              ["tag", "Тег"],
              ["label", "Метка key:value"],
              ["requirement", "Requirement"],
              ["factor", "Фактор"],
              ["factorState", "Состояние фактора"],
              ["tier", "Уровень верификации"],
              ["proxy", "Прокси: id или хост"],
              ["quotaKind", "Вид операции для квоты"],
            ] as const
          ).map(([key, label]) => (
            <TextFilter
              key={key}
              label={label}
              value={value[key]}
              onChange={(next) => {
                patch(key, next);
              }}
            />
          ))}
          <FilterSelect
            label="Есть связь"
            options={BOOLEAN}
            value={value.linked}
            onChange={(next) => {
              patch("linked", next);
            }}
          />
          {(
            [
              ["createdFrom", "Добавлен с"],
              ["createdTo", "Добавлен до"],
              ["lastLeasedFrom", "Последняя аренда с"],
              ["lastLeasedTo", "Последняя аренда до"],
              ["syncedFrom", "Сверен с"],
              ["syncedTo", "Сверен до"],
              ["standingChangedFrom", "Standing изменён с"],
              ["standingChangedTo", "Standing изменён до"],
            ] as const
          ).map(([key, label]) => (
            <TextFilter
              key={key}
              label={label}
              type="datetime-local"
              value={value[key]}
              onChange={(next) => {
                patch(key, next);
              }}
            />
          ))}
        </div>
      </details>
    </div>
  );
}

export function AccountColumnPicker({
  columns,
  selected,
  onChange,
}: {
  columns: Array<{ id: string; label: string }>;
  selected: ReadonlySet<string>;
  onChange: (selected: ReadonlySet<string>) => void;
}) {
  return (
    <details className="relative">
      <summary className="cursor-pointer text-sm font-medium">Колонки</summary>
      <div className="bg-popover absolute right-0 z-10 mt-2 grid min-w-56 gap-2 rounded-md border p-3 shadow-md">
        {columns.map((column) => (
          <label className="flex items-center gap-2 text-sm" key={column.id}>
            <input
              checked={selected.has(column.id)}
              className="accent-primary size-4"
              type="checkbox"
              onChange={(event) => {
                const next = new Set(selected);
                if (event.target.checked) next.add(column.id);
                else next.delete(column.id);
                onChange(next);
              }}
            />
            {column.label}
          </label>
        ))}
      </div>
    </details>
  );
}
