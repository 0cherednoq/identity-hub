import type { LeaseSearch, LeaseSearchPatch } from "@/shared/lib/lease-search";
import { Card, CardContent } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";

export function LeaseControls({
  value,
  onPatch,
}: {
  value: LeaseSearch;
  onPatch: (patch: LeaseSearchPatch) => void;
}) {
  return (
    <Card>
      <CardContent className="grid gap-4 pt-6 md:grid-cols-4">
        <div className="grid gap-2">
          <label className="text-sm font-medium" htmlFor="lease-service">
            Сервис
          </label>
          <Input
            id="lease-service"
            value={value.service ?? ""}
            onChange={(event) => {
              onPatch({ service: event.target.value || undefined });
            }}
            placeholder="fake_tg"
          />
        </div>
        <div className="grid gap-2">
          <label className="text-sm font-medium" htmlFor="lease-pool">
            Пул
          </label>
          <Input
            id="lease-pool"
            value={value.pool ?? ""}
            onChange={(event) => {
              onPatch({ pool: event.target.value || undefined });
            }}
            placeholder="workers"
          />
        </div>
        <div className="grid gap-2">
          <label className="text-sm font-medium" htmlFor="lease-holder">
            Holder
          </label>
          <Input
            id="lease-holder"
            value={value.holder ?? ""}
            onChange={(event) => {
              onPatch({ holder: event.target.value || undefined });
            }}
            placeholder="worker-01"
          />
        </div>
        <div className="grid gap-2">
          <span className="text-sm font-medium">Разрез</span>
          <Select
            value={value.groupBy ?? "pool"}
            onValueChange={(groupBy) => {
              if (
                groupBy === "pool" ||
                groupBy === "service" ||
                groupBy === "holder"
              ) {
                onPatch({ groupBy });
              }
            }}
          >
            <SelectTrigger aria-label="Разрез">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="pool">По пулу</SelectItem>
              <SelectItem value="service">По сервису</SelectItem>
              <SelectItem value="holder">По holder</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </CardContent>
    </Card>
  );
}

export function LeaseUpdatedAt({ value }: { value: string }) {
  return (
    <span className="text-muted-foreground text-xs">
      Срез: {new Date(value).toLocaleTimeString("ru-RU")}
    </span>
  );
}
