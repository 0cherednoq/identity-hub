import type { ColumnDef } from "@tanstack/react-table";
import { Moon, RefreshCw, Sun } from "lucide-react";
import type { ReactNode } from "react";

import { TimeSeriesChart } from "@/shared/components/charts/time-series-chart";
import {
  DataTable,
  NumberCell,
} from "@/shared/components/data-table/data-table";
import { Section } from "@/shared/components/layout/layout";
import { MetricCard, MetricGrid } from "@/shared/components/metrics/metrics";
import { QueryState } from "@/shared/components/states/query-state";
import { StatusBadge } from "@/shared/components/status/status-badge";
import { DashboardPage } from "@/shared/components/templates/templates";
import { Button } from "@/shared/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";
import { Separator } from "@/shared/ui/separator";
import { Skeleton } from "@/shared/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs";

interface CatalogRow {
  id: string;
  account: string;
  status: ReactNode;
  leases: number;
}

const columns: ColumnDef<CatalogRow>[] = [
  {
    accessorKey: "id",
    header: "ID",
    cell: ({ getValue }) => (
      <code className="font-mono text-xs">{String(getValue())}</code>
    ),
  },
  { accessorKey: "account", header: "Аккаунт" },
  {
    accessorKey: "status",
    header: "Статус",
    cell: ({ getValue }) => getValue() as ReactNode,
  },
  {
    accessorKey: "leases",
    header: "Аренды",
    cell: ({ getValue }) => <NumberCell value={Number(getValue())} />,
  },
];

const rows: CatalogRow[] = [
  {
    id: "acc_01JZY8PZ7N2DJYYXQ08Z2EP6HC",
    account: "Короткое имя",
    status: <StatusBadge tone="success">Готов</StatusBadge>,
    leases: 0,
  },
  {
    id: "acc_01JZY8V1MQQS0G2MJPFK3HQG74",
    account:
      "Очень длинное имя аккаунта для проверки переполнения и горизонтальной прокрутки таблицы",
    status: <StatusBadge tone="warning">Остывает</StatusBadge>,
    leases: 1_000_000,
  },
  {
    id: "acc_01JZY9102NBJGCAPZ8ZPXCBKDN",
    account: "Выбранная строка",
    status: <StatusBadge tone="danger">Карантин</StatusBadge>,
    leases: 42,
  },
];

const chartData = [
  { label: "00:00", value: 18 },
  { label: "04:00", value: 32 },
  { label: "08:00", value: 27 },
  { label: "12:00", value: 54 },
  { label: "16:00", value: 46 },
  { label: "20:00", value: 71 },
];

function TokenPalette() {
  const tokens = [
    ["primary", "bg-primary text-primary-foreground"],
    ["secondary", "bg-secondary text-secondary-foreground"],
    ["muted", "bg-muted text-muted-foreground"],
    ["info", "bg-info text-info-foreground"],
    ["success", "bg-success text-success-foreground"],
    ["warning", "bg-warning text-warning-foreground"],
    ["destructive", "bg-destructive text-destructive-foreground"],
  ] as const;
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {tokens.map(([label, className]) => (
        <div className={`${className} rounded-lg p-4 text-sm`} key={label}>
          {label}
        </div>
      ))}
    </div>
  );
}

function TemplateGallery() {
  const templates = [
    ["ListPage", "Фильтры → таблица → пагинация"],
    ["DetailPage", "Шапка → вкладки → секции"],
    ["DashboardPage", "Метрики → графики → таблицы"],
  ] as const;
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      {templates.map(([title, description]) => (
        <Card key={title}>
          <CardHeader>
            <CardTitle>{title}</CardTitle>
            <CardDescription>{description}</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2">
            <Skeleton className="h-8 w-full" />
            <Separator />
            <Skeleton className="h-16 w-full" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function DesignCatalog({
  theme,
  onThemeChange,
  domainStatuses,
}: {
  theme: "dark" | "light";
  onThemeChange: () => void;
  domainStatuses: ReactNode;
}) {
  return (
    <DashboardPage
      eyebrow="Внутренний каталог"
      title="Дизайн-система"
      description="Живой контракт компонентов, тем, состояний и граничных данных."
      actions={
        <Button type="button" variant="outline" onClick={onThemeChange}>
          {theme === "light" ? <Moon /> : <Sun />}
          {theme === "light" ? "Тёмная тема" : "Светлая тема"}
        </Button>
      }
    >
      <MetricGrid>
        <MetricCard label="Нулевое значение" value={0} description="Граница" />
        <MetricCard
          label="Большое значение"
          value={1_000_000}
          description="Форматирование"
        />
        <MetricCard label="Активные" value={258} description="Демо-набор" />
        <MetricCard label="Ошибки" value={3} description="За 24 часа" />
      </MetricGrid>

      <Section title="Токены" description="Семантическая палитра текущей темы">
        <TokenPalette />
      </Section>

      <Section title="Шаблоны страниц">
        <TemplateGallery />
      </Section>

      <Section title="Примитивы" description="Варианты кнопок, поля и вкладки">
        <div className="grid gap-6">
          <div className="flex flex-wrap items-center gap-2">
            <Button>Основная</Button>
            <Button variant="secondary">Вторичная</Button>
            <Button variant="outline">Контурная</Button>
            <Button variant="ghost">Призрачная</Button>
            <Button variant="destructive">Опасная</Button>
            <Button size="sm">Компактная</Button>
            <Button disabled>Недоступна</Button>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <Input aria-label="Обычное поле" placeholder="Обычное поле" />
            <Input
              aria-label="Поле с ошибкой"
              aria-invalid="true"
              value="Ошибка"
              readOnly
            />
            <Input
              aria-label="Недоступное поле"
              placeholder="Недоступно"
              disabled
            />
          </div>
          <Tabs defaultValue="first">
            <TabsList>
              <TabsTrigger value="first">Активная</TabsTrigger>
              <TabsTrigger value="second">Вторая</TabsTrigger>
              <TabsTrigger value="disabled" disabled>
                Недоступна
              </TabsTrigger>
            </TabsList>
            <TabsContent value="first">
              Содержимое активной вкладки.
            </TabsContent>
            <TabsContent value="second">Содержимое второй вкладки.</TabsContent>
          </Tabs>
        </div>
      </Section>

      <Section title="Статусы" description="Все тона и все доменные enum">
        <div className="grid gap-4">
          <div className="flex flex-wrap gap-2">
            <StatusBadge tone="neutral">Neutral</StatusBadge>
            <StatusBadge tone="info">Info</StatusBadge>
            <StatusBadge tone="success">Success</StatusBadge>
            <StatusBadge tone="warning">Warning</StatusBadge>
            <StatusBadge tone="danger">Danger</StatusBadge>
            <StatusBadge tone="danger" unknown>
              Неизвестно: backend_new_state
            </StatusBadge>
          </div>
          <Separator />
          {domainStatuses}
        </div>
      </Section>

      <Section title="Состояния запросов">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle>Загрузка</CardTitle>
            </CardHeader>
            <CardContent>
              <QueryState
                query={{ isPending: true, isError: false }}
                skeleton={<Skeleton className="h-24 w-full" />}
                empty={{ title: "Пусто", description: "Нет данных" }}
                isEmpty={() => false}
              >
                {() => null}
              </QueryState>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Пусто</CardTitle>
            </CardHeader>
            <CardContent>
              <QueryState
                query={{ data: [], isPending: false, isError: false }}
                skeleton={<Skeleton className="h-24 w-full" />}
                empty={{
                  title: "Аккаунтов нет",
                  description: "Измените фильтры или импортируйте аккаунт.",
                }}
                isEmpty={(data) => data.length === 0}
              >
                {() => null}
              </QueryState>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Ошибка</CardTitle>
            </CardHeader>
            <CardContent>
              <QueryState
                query={{
                  error: new Error("HTTP 503 · backend_unavailable"),
                  isPending: false,
                  isError: true,
                  refetch: () => undefined,
                }}
                skeleton={<Skeleton className="h-24 w-full" />}
                empty={{ title: "Пусто", description: "Нет данных" }}
                isEmpty={() => false}
              >
                {() => null}
              </QueryState>
            </CardContent>
          </Card>
        </div>
      </Section>

      <Section
        title="DataTable"
        description="Длинный текст, 0, 1 000 000 и выбранная строка"
        actions={
          <Button type="button" size="sm" variant="outline">
            <RefreshCw /> Обновить
          </Button>
        }
      >
        <DataTable
          columns={columns}
          data={rows}
          density="compact"
          getRowId={(row) => row.id}
          selectedRowIds={new Set([rows[2]?.id ?? ""])}
        />
      </Section>

      <Section title="График" description="Wrapper Recharts на chart-токенах">
        <TimeSeriesChart data={chartData} />
      </Section>
    </DashboardPage>
  );
}
