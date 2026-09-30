import type {
  HumanQueueSearch,
  HumanQueueSearchPatch,
} from "@/shared/lib/human-queue-search";
import { Card, CardContent } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/select";

export function HumanQueueControls({
  value,
  onPatch,
}: {
  value: HumanQueueSearch;
  onPatch: (patch: HumanQueueSearchPatch) => void;
}) {
  return (
    <Card>
      <CardContent className="grid gap-4 pt-6 md:grid-cols-3">
        <div className="grid gap-2">
          <label className="text-sm font-medium" htmlFor="queue-service">
            Сервис
          </label>
          <Input
            id="queue-service"
            value={value.service ?? ""}
            onChange={(event) => {
              onPatch({ service: event.target.value || undefined });
            }}
            placeholder="fake_exchange"
          />
        </div>
        <div className="grid gap-2">
          <span className="text-sm font-medium">Раздел</span>
          <Select
            value={value.kind ?? "all"}
            onValueChange={(kind) => {
              onPatch({
                kind:
                  kind === "all"
                    ? undefined
                    : (kind as HumanQueueSearch["kind"]),
              });
            }}
          >
            <SelectTrigger aria-label="Раздел очереди">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все разделы</SelectItem>
              <SelectItem value="challenge">Challenges</SelectItem>
              <SelectItem value="requirement">Требования</SelectItem>
              <SelectItem value="manual_review">Ручная проверка</SelectItem>
              <SelectItem value="drift">Drift после override</SelectItem>
              <SelectItem value="quarantine">Карантин</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-2">
          <span className="text-sm font-medium">Срочность</span>
          <Select
            value={value.urgency ?? "all"}
            onValueChange={(urgency) => {
              onPatch({
                urgency:
                  urgency === "all"
                    ? undefined
                    : (urgency as HumanQueueSearch["urgency"]),
              });
            }}
          >
            <SelectTrigger aria-label="Срочность">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Любая</SelectItem>
              <SelectItem value="critical">Критическая</SelectItem>
              <SelectItem value="high">Высокая</SelectItem>
              <SelectItem value="medium">Средняя</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </CardContent>
    </Card>
  );
}

export function HumanQueueUpdatedAt({ value }: { value: number }) {
  return (
    <span className="text-muted-foreground text-xs">
      Обновлено: {new Date(value).toLocaleTimeString("ru-RU")}
    </span>
  );
}
