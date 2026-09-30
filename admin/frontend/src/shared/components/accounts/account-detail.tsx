import type {
  AccountHeaderResponse,
  AccountSectionResponse,
} from "@/shared/api/generated/types.gen";
import type { ReactNode } from "react";
import type { AccountSection } from "@/shared/api/accounts";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import { Tabs, TabsList, TabsTrigger } from "@/shared/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";
import { StatusBadge } from "@/shared/components/status/status-badge";

const ACCOUNT_TABS: Array<{ label: string; value: AccountSection }> = [
  { label: "Состояние", value: "state" },
  { label: "Профиль и данные", value: "profile" },
  { label: "Доступы и сессии", value: "access" },
  { label: "Верификации", value: "verification" },
  { label: "Аренды и квоты", value: "leases" },
  { label: "Прокси и связи", value: "network" },
  { label: "События", value: "events" },
  { label: "Логи аренд", value: "logs" },
];

export function AccountHeaderDisplay({
  value,
  statuses,
}: {
  value: AccountHeaderResponse;
  statuses: ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          {value.service} · {value.external_ref}
          <Badge variant="outline">revision {value.revision}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="flex flex-wrap items-center gap-2">
          {statuses}
          <StatusBadge tone={value.eligible ? "success" : "warning"}>
            {value.eligible ? "Пригоден" : "Не пригоден"}
          </StatusBadge>
        </div>
        {value.blockers.length === 0 ? null : (
          <p className="text-muted-foreground text-sm">
            {value.blockers.join(" · ")}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <code>{value.id}</code>
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              void navigator.clipboard.writeText(value.id);
            }}
          >
            Копировать ID
          </Button>
          <span>
            Добавлен: {new Date(value.created_at).toLocaleString("ru-RU")}
          </span>
        </div>
        {value.lease === null ? (
          <Badge variant="outline">Нет живой аренды</Badge>
        ) : (
          <p className="text-sm">
            Аренда: {String(value.lease.pool)} · {String(value.lease.holder)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export function AccountTabs({
  value,
  onChange,
}: {
  value: AccountSection;
  onChange: (value: AccountSection) => void;
}) {
  return (
    <Tabs
      value={value}
      onValueChange={(next) => {
        onChange(next as AccountSection);
      }}
    >
      <TabsList
        className="h-auto max-w-full flex-wrap justify-start"
        variant="line"
      >
        {ACCOUNT_TABS.map((tab) => (
          <TabsTrigger key={tab.value} value={tab.value}>
            {tab.label}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asRows(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.map(asRecord) : [];
}

function displayValue(value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") {
    return <span className="text-muted-foreground">—</span>;
  }
  if (typeof value === "boolean") {
    return value ? "Да" : "Нет";
  }
  if (typeof value === "string") {
    const timestamp = Date.parse(value);
    return Number.isNaN(timestamp) || !value.includes("T")
      ? value
      : new Date(timestamp).toLocaleString("ru-RU");
  }
  if (typeof value === "number") {
    return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 }).format(
      value,
    );
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? "—" : value.map(String).join(", ");
  }
  return <code className="text-xs">{JSON.stringify(value)}</code>;
}

function FieldGrid({ value }: { value: unknown }) {
  const entries = Object.entries(asRecord(value));
  if (entries.length === 0) {
    return <p className="text-muted-foreground text-sm">Нет данных</p>;
  }
  return (
    <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 xl:grid-cols-3">
      {entries.map(([key, item]) => (
        <div className="min-w-0" key={key}>
          <dt className="text-muted-foreground text-xs font-medium uppercase">
            {key.replaceAll("_", " ")}
          </dt>
          <dd className="mt-1 text-sm break-words">{displayValue(item)}</dd>
        </div>
      ))}
    </dl>
  );
}

function RecordsTable({ value }: { value: unknown }) {
  const rows = asRows(value);
  const columns = Array.from(new Set(rows.flatMap((row) => Object.keys(row))));
  if (rows.length === 0) {
    return <p className="text-muted-foreground text-sm">Нет записей</p>;
  }
  return (
    <div className="overflow-x-auto rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            {columns.map((column) => (
              <TableHead className="whitespace-nowrap" key={column}>
                {column.replaceAll("_", " ")}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row, index) => (
            <TableRow key={`${index.toString()}:${JSON.stringify(row)}`}>
              {columns.map((column) => (
                <TableCell className="max-w-80 align-top" key={column}>
                  {displayValue(row[column])}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function SectionBlock({
  children,
  title,
}: {
  children: ReactNode;
  title: string;
}) {
  return (
    <section className="grid gap-3">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="bg-muted max-h-96 overflow-auto rounded-md p-4 text-xs whitespace-pre-wrap">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function SectionData({
  section,
  value,
}: {
  section: AccountSection;
  value: unknown;
}) {
  const data = asRecord(value);
  if (section === "state") {
    return <RecordsTable value={data.regions} />;
  }
  if (section === "profile") {
    return (
      <div className="grid gap-6">
        <SectionBlock title="Профиль">
          <FieldGrid value={data.profile} />
        </SectionBlock>
        <SectionBlock title="Атрибуты">
          <FieldGrid value={data.attributes} />
        </SectionBlock>
        <SectionBlock title="Метки, теги и заметка">
          <FieldGrid
            value={{ labels: data.labels, tags: data.tags, note: data.note }}
          />
        </SectionBlock>
        <SectionBlock title="Пространства данных">
          <RecordsTable value={data.data_namespaces} />
          {data.namespace_contents_available === false ? (
            <p className="text-muted-foreground text-sm">
              {displayValue(
                data.namespace_contents_reason ?? "Содержимое недоступно.",
              )}
            </p>
          ) : null}
        </SectionBlock>
      </div>
    );
  }
  if (section === "access") {
    return (
      <div className="grid gap-6">
        <SectionBlock title="Credentials (только metadata)">
          <FieldGrid value={data.credential} />
          <Badge className="w-fit" variant="outline">
            Значения секретов не загружены
          </Badge>
        </SectionBlock>
        <SectionBlock title="Сессии">
          <RecordsTable value={data.sessions} />
        </SectionBlock>
      </div>
    );
  }
  if (section === "verification") {
    return (
      <div className="grid gap-6">
        <SectionBlock title="Уровни">
          <RecordsTable value={data.tiers} />
        </SectionBlock>
        <SectionBlock title="Факторы">
          <RecordsTable value={data.factors} />
        </SectionBlock>
        <SectionBlock title="Требования">
          <RecordsTable value={data.requirements} />
        </SectionBlock>
        <SectionBlock title="Открытый challenge">
          <FieldGrid value={data.challenge} />
        </SectionBlock>
      </div>
    );
  }
  if (section === "leases") {
    const stats = asRecord(data.stats);
    return (
      <div className="grid gap-6">
        <SectionBlock title="Текущая аренда">
          <FieldGrid value={data.current} />
        </SectionBlock>
        <SectionBlock title="Статистика">
          <RecordsTable
            value={Object.entries(stats).map(([window, row]) => ({
              window,
              ...asRecord(row),
            }))}
          />
        </SectionBlock>
        <SectionBlock title="Квоты">
          <RecordsTable value={data.quotas} />
        </SectionBlock>
        <SectionBlock title="История">
          <RecordsTable value={data.history} />
        </SectionBlock>
      </div>
    );
  }
  if (section === "network") {
    return (
      <div className="grid gap-6">
        <SectionBlock title="Прокси">
          <FieldGrid value={data.proxy} />
        </SectionBlock>
        <SectionBlock title="Зависит от">
          <RecordsTable value={data.outgoing} />
        </SectionBlock>
        <SectionBlock title="От него зависят">
          <RecordsTable value={data.incoming} />
        </SectionBlock>
        <SectionBlock title="Анализ последствий">
          <RecordsTable value={data.impact} />
        </SectionBlock>
      </div>
    );
  }
  return <JsonBlock value={value} />;
}

export function AccountSectionPanel({
  value,
}: {
  value: AccountSectionResponse;
}) {
  if (!value.available) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Источник данных ещё не подключён</CardTitle>
        </CardHeader>
        <CardContent className="text-muted-foreground text-sm">
          {value.reason}
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {ACCOUNT_TABS.find((tab) => tab.value === value.section)?.label ??
            value.section}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <SectionData section={value.section} value={value.data} />
      </CardContent>
    </Card>
  );
}
