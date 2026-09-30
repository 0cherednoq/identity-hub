import type { EventTypeResponse } from "@/shared/api/generated/types.gen";
import type { EventSearch, EventSearchPatch } from "@/shared/lib/event-search";
import { Card, CardContent } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";

function localDateTime(value: string | undefined): string {
  if (value === undefined) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString().slice(0, 16);
}

function EventSelect({
  label,
  value,
  items,
  onChange,
}: {
  label: string;
  value: string;
  items: Array<[string, string]>;
  onChange: (value: string) => void;
}) {
  return (
    <div className="grid gap-2">
      <span className="text-sm font-medium">{label}</span>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger aria-label={label}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {items.map(([itemValue, itemLabel]) => (
            <SelectItem key={itemValue} value={itemValue}>
              {itemLabel}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

export function EventControls({
  value,
  groups,
  types,
  onPatch,
}: {
  value: EventSearch;
  groups: string[];
  types: EventTypeResponse[];
  onPatch: (patch: EventSearchPatch) => void;
}) {
  const textFields: Array<[keyof EventSearch, string, string]> = [
    ["service", "Сервис", "demo_account"],
    ["actor", "Actor", "worker-17"],
    ["identityId", "ID аккаунта", "UUID"],
    ["proxyId", "ID прокси", "UUID"],
    ["leaseId", "ID аренды", "UUID"],
  ];
  return (
    <Card>
      <CardContent className="grid gap-4 pt-6 md:grid-cols-2 xl:grid-cols-4">
        <EventSelect
          label="Группа"
          value={value.group ?? "all"}
          items={[
            ["all", "Все группы"],
            ...groups.map((item) => [item, item] as [string, string]),
          ]}
          onChange={(group) => {
            onPatch({
              group: group === "all" ? undefined : group,
              type: undefined,
            });
          }}
        />
        <EventSelect
          label="Тип события"
          value={value.type ?? "all"}
          items={[
            ["all", "Все типы"],
            ...types
              .filter(
                (item) =>
                  value.group === undefined || item.group === value.group,
              )
              .filter((item) => value.includeJournal === true || item.external)
              .map((item) => [item.type, item.type] as [string, string]),
          ]}
          onChange={(type) => {
            onPatch({ type: type === "all" ? undefined : type });
          }}
        />
        <EventSelect
          label="Важность"
          value={value.severity ?? "all"}
          items={[
            ["all", "Любая"],
            ["info", "Info"],
            ["warning", "Warning"],
            ["critical", "Critical"],
          ]}
          onChange={(severity) => {
            onPatch({
              severity:
                severity === "all"
                  ? undefined
                  : (severity as EventSearch["severity"]),
            });
          }}
        />
        <label className="flex items-end gap-2 pb-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={value.includeJournal ?? false}
            onChange={(event) => {
              onPatch({
                includeJournal: event.target.checked || undefined,
                type: undefined,
              });
            }}
          />
          Включая журнальные
        </label>
        {textFields.map(([key, label, placeholder]) => (
          <label key={key} className="grid gap-2 text-sm font-medium">
            {label}
            <Input
              aria-label={label}
              value={String(value[key] ?? "")}
              placeholder={placeholder}
              onChange={(event) => {
                onPatch({ [key]: event.target.value || undefined });
              }}
            />
          </label>
        ))}
        {(["occurredFrom", "occurredTo"] as const).map((key) => (
          <label key={key} className="grid gap-2 text-sm font-medium">
            {key === "occurredFrom" ? "События с" : "События до"}
            <Input
              aria-label={key === "occurredFrom" ? "События с" : "События до"}
              type="datetime-local"
              value={localDateTime(value[key])}
              onChange={(event) => {
                onPatch({
                  [key]:
                    event.target.value === ""
                      ? undefined
                      : new Date(event.target.value).toISOString(),
                });
              }}
            />
          </label>
        ))}
      </CardContent>
    </Card>
  );
}
