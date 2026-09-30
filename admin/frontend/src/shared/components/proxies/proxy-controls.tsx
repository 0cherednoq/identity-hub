import type { ProxySearch, ProxySearchPatch } from "@/shared/lib/proxy-search";
import { Card, CardContent } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";

export function ProxyControls({
  value,
  onPatch,
}: {
  value: ProxySearch;
  onPatch: (patch: ProxySearchPatch) => void;
}) {
  return (
    <Card>
      <CardContent className="grid gap-4 pt-6 md:grid-cols-3">
        <div className="grid gap-2">
          <label className="text-sm font-medium" htmlFor="proxy-pool">
            Пул
          </label>
          <Input
            id="proxy-pool"
            value={value.pool ?? ""}
            onChange={(event) => {
              onPatch({ pool: event.target.value || undefined });
            }}
            placeholder="residential-ua"
          />
        </div>
        <ProxySelect
          label="Статус"
          value={value.status ?? "all"}
          items={[
            ["all", "Все"],
            ["alive", "Alive"],
            ["quarantined", "Карантин"],
            ["dead", "Dead"],
            ["unknown", "Неизвестно"],
          ]}
          onChange={(status) => {
            onPatch({
              status:
                status === "all"
                  ? undefined
                  : (status as ProxySearch["status"]),
            });
          }}
        />
        <ProxySelect
          label="Ёмкость"
          value={value.capacity ?? "all"}
          items={[
            ["all", "Любая"],
            ["available", "Есть место"],
            ["full", "Заполнен"],
            ["unbound", "Без привязок"],
          ]}
          onChange={(capacity) => {
            onPatch({
              capacity:
                capacity === "all"
                  ? undefined
                  : (capacity as ProxySearch["capacity"]),
            });
          }}
        />
      </CardContent>
    </Card>
  );
}

function ProxySelect({
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
