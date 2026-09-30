import type { AccountListItemResponse } from "@/shared/api/generated/types.gen";
import { Badge } from "@/shared/ui/badge";
import { StatusBadge } from "@/shared/components/status/status-badge";

export function EligibilityCell({ row }: { row: AccountListItemResponse }) {
  return row.eligible ? (
    <StatusBadge tone="success">Пригоден</StatusBadge>
  ) : (
    <div className="grid gap-1">
      <StatusBadge tone="warning">Не пригоден</StatusBadge>
      <span className="text-muted-foreground max-w-56 text-xs whitespace-normal">
        {row.blockers.join(" · ") || "Причина не определена"}
      </span>
    </div>
  );
}

export function LeaseCell({ row }: { row: AccountListItemResponse }) {
  if (row.lease === null) return <Badge variant="outline">Свободен</Badge>;
  return (
    <div className="grid gap-1">
      <StatusBadge tone="info">{row.lease.pool}</StatusBadge>
      <span className="text-muted-foreground text-xs">{row.lease.holder}</span>
      <DateCell value={row.lease.acquired_at} />
    </div>
  );
}

export function ProxyCell({ row }: { row: AccountListItemResponse }) {
  if (row.proxy === null) return <span>—</span>;
  return (
    <div className="grid gap-1">
      <span className="font-mono text-sm">
        {row.proxy.host}:{row.proxy.port}
      </span>
      <Badge variant="outline">{row.proxy.status}</Badge>
    </div>
  );
}

export function TagsCell({ row }: { row: AccountListItemResponse }) {
  const values = [
    ...row.tags,
    ...Object.entries(row.labels).map(
      ([key, value]) => `${key}:${String(value)}`,
    ),
  ];
  if (values.length === 0) return <span>—</span>;
  return (
    <div className="flex max-w-64 flex-wrap gap-1">
      {values.slice(0, 6).map((value) => (
        <Badge key={value} variant="outline">
          {value}
        </Badge>
      ))}
    </div>
  );
}

export function DateCell({ value }: { value: string | null }) {
  if (value === null) return <span>—</span>;
  return (
    <time className="text-sm whitespace-nowrap" dateTime={value}>
      {new Intl.DateTimeFormat("ru-RU", {
        dateStyle: "short",
        timeStyle: "short",
      }).format(new Date(value))}
    </time>
  );
}

export function AccountListSummary({
  selected,
  total,
}: {
  selected: number;
  total: string | null;
}) {
  return (
    <div className="text-muted-foreground flex flex-wrap items-center gap-3 text-sm">
      <span>Найдено: {total ?? "не подсчитано"}</span>
      <span>Выбрано: {selected}</span>
      {selected === 0 ? null : (
        <span>
          Выбор сохранён для будущих массовых операций; мутации отключены.
        </span>
      )}
    </div>
  );
}
