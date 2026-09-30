import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";

import { fetchPrincipal } from "@/shared/api/auth";
import { fetchDeliveryCenter } from "@/shared/api/delivery-center";
import type {
  DeliveryCenterResponse,
  NotificationDeliveryResponse,
  WebhookDeliveryResponse,
} from "@/shared/api/generated/types.gen";
import { POLL_INTERVAL } from "@/shared/api/query-client";
import { QueryState } from "@/shared/components/states/query-state";
import {
  IntegrationCreateControls,
  IntegrationRowActions,
  RetryDeliveryButton,
} from "@/shared/components/commands/integration-controls";
import { ListPage } from "@/shared/components/templates/templates";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import { Skeleton } from "@/shared/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs";

function Status({ value }: { value: string }) {
  return (
    <Badge
      variant={
        value === "succeeded" || value === "enabled"
          ? "secondary"
          : value === "failed" || value === "dead"
            ? "destructive"
            : "outline"
      }
    >
      {value}
    </Badge>
  );
}

function JournalPager({
  hasPrevious,
  nextCursor,
  onNext,
}: {
  hasPrevious: boolean;
  nextCursor: string | null;
  onNext: (cursor: string) => void;
}) {
  return (
    <div className="flex justify-end gap-2">
      <Button
        type="button"
        variant="outline"
        disabled={!hasPrevious}
        onClick={() => {
          window.history.back();
        }}
      >
        Назад
      </Button>
      <Button
        type="button"
        disabled={nextCursor === null}
        onClick={() => {
          if (nextCursor !== null) onNext(nextCursor);
        }}
      >
        Далее
      </Button>
    </div>
  );
}

function TelegramTab({
  data,
  isAdmin,
  spaceSlug,
}: {
  data: DeliveryCenterResponse;
  isAdmin: boolean;
  spaceSlug: string;
}) {
  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Telegram-каналы · {data.channels.length}</CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Канал</TableHead>
                <TableHead>Получатель</TableHead>
                <TableHead>Состояние</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.channels.map((channel) => (
                <TableRow key={channel.id}>
                  <TableCell>{channel.title}</TableCell>
                  <TableCell>
                    {channel.masked_target}
                    {channel.thread_id === null
                      ? ""
                      : ` · thread ${String(channel.thread_id)}`}
                  </TableCell>
                  <TableCell>
                    <Status value={channel.enabled ? "enabled" : "disabled"} />
                  </TableCell>
                  <TableCell>
                    {channel.actions_available ? (
                      <IntegrationRowActions
                        enabled={channel.enabled}
                        id={channel.id}
                        isAdmin={isAdmin}
                        kind="channel"
                        spaceSlug={spaceSlug}
                      />
                    ) : (
                      <span title={channel.actions_unavailable_reason}>—</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Правила · {data.rules.length}</CardTitle>
          <p>
            Предпросмотр считает совпадения с{" "}
            {new Date(data.preview_window_started_at).toLocaleString("ru-RU")} и
            не двигает курсор диспетчера.
          </p>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Правило</TableHead>
                <TableHead>События / важность</TableHead>
                <TableHead>Фильтр / дайджест</TableHead>
                <TableHead>Совпадений за 24 ч</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.rules.map((rule) => (
                <TableRow key={rule.id}>
                  <TableCell>
                    <p>{rule.name}</p>
                    <Status value={rule.enabled ? "enabled" : "disabled"} />
                  </TableCell>
                  <TableCell>
                    <p>{rule.event_types.join(", ")}</p>
                    <Badge variant="outline">{rule.min_severity}</Badge>
                  </TableCell>
                  <TableCell>
                    <code>{JSON.stringify(rule.filter)}</code>
                    <p>
                      {rule.digest_seconds === null
                        ? "сразу"
                        : `${String(rule.digest_seconds)} с`}
                    </p>
                  </TableCell>
                  <TableCell>{rule.preview_24h}</TableCell>
                  <TableCell>
                    {rule.actions_available ? (
                      <IntegrationRowActions
                        enabled={rule.enabled}
                        id={rule.id}
                        isAdmin={isAdmin}
                        kind="rule"
                        spaceSlug={spaceSlug}
                      />
                    ) : (
                      <span title={rule.actions_unavailable_reason}>—</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}

function NotificationJournal({
  items,
  nextCursor,
  hasPrevious,
  onNext,
  spaceSlug,
}: {
  items: NotificationDeliveryResponse[];
  nextCursor: string | null;
  hasPrevious: boolean;
  onNext: (cursor: string) => void;
  spaceSlug: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Журнал уведомлений · {items.length}</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4 overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Время</TableHead>
              <TableHead>Правило / канал</TableHead>
              <TableHead>События</TableHead>
              <TableHead>Состояние</TableHead>
              <TableHead>Ошибка</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => (
              <TableRow key={item.id}>
                <TableCell className="whitespace-nowrap">
                  {new Date(item.sent_at ?? item.scheduled_at).toLocaleString(
                    "ru-RU",
                  )}
                </TableCell>
                <TableCell>
                  <p>{item.rule_name}</p>
                  <p>{item.channel_title}</p>
                </TableCell>
                <TableCell>{item.event_count}</TableCell>
                <TableCell>
                  <Status value={item.status} />
                  <p>попытка {item.attempt}</p>
                </TableCell>
                <TableCell>{item.error_summary ?? "—"}</TableCell>
                <TableCell>
                  {item.status === "failed" || item.status === "dead" ? (
                    <RetryDeliveryButton
                      deliveryId={item.id}
                      deliveryKind="notification"
                      spaceSlug={spaceSlug}
                    />
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <JournalPager
          hasPrevious={hasPrevious}
          nextCursor={nextCursor}
          onNext={onNext}
        />
      </CardContent>
    </Card>
  );
}

function WebhooksTab({
  data,
  hasPrevious,
  isAdmin,
  onNext,
  spaceSlug,
}: {
  data: DeliveryCenterResponse;
  hasPrevious: boolean;
  isAdmin: boolean;
  onNext: (cursor: string) => void;
  spaceSlug: string;
}) {
  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader>
          <CardTitle>
            Webhook endpoints · {data.webhook_endpoints.length}
          </CardTitle>
          <p>
            Retry: от {data.retry_policy.base_seconds} с экспоненциальной
            задержкой до {data.retry_policy.cap_seconds} с, максимум{" "}
            {data.retry_policy.max_attempts} попыток. После лимита endpoint
            отключается. Ручной retry сбрасывает попытки и снова включает
            endpoint.
          </p>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Назначение</TableHead>
                <TableHead>События / фильтр</TableHead>
                <TableHead>Состояние</TableHead>
                <TableHead>Сбои</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.webhook_endpoints.map((endpoint) => (
                <TableRow key={endpoint.id}>
                  <TableCell>
                    <p>{endpoint.masked_target}</p>
                    <p>{endpoint.description ?? "—"}</p>
                  </TableCell>
                  <TableCell>
                    <p>{endpoint.event_types.join(", ")}</p>
                    <code>{JSON.stringify(endpoint.filter)}</code>
                  </TableCell>
                  <TableCell>
                    <Status value={endpoint.enabled ? "enabled" : "disabled"} />
                    <p>{endpoint.disabled_reason ?? "—"}</p>
                  </TableCell>
                  <TableCell>{endpoint.failure_streak}</TableCell>
                  <TableCell>
                    {endpoint.actions_available ? (
                      <IntegrationRowActions
                        enabled={endpoint.enabled}
                        id={endpoint.id}
                        isAdmin={isAdmin}
                        kind="webhook"
                        spaceSlug={spaceSlug}
                      />
                    ) : (
                      <span title={endpoint.actions_unavailable_reason}>—</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <WebhookJournal
        items={data.webhook_deliveries}
        nextCursor={data.webhook_next_cursor}
        hasPrevious={hasPrevious}
        onNext={onNext}
        spaceSlug={spaceSlug}
      />
    </div>
  );
}

function WebhookJournal({
  items,
  nextCursor,
  hasPrevious,
  onNext,
  spaceSlug,
}: {
  items: WebhookDeliveryResponse[];
  nextCursor: string | null;
  hasPrevious: boolean;
  onNext: (cursor: string) => void;
  spaceSlug: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Журнал webhook-доставок · {items.length}</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4 overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Создана</TableHead>
              <TableHead>Событие</TableHead>
              <TableHead>Состояние</TableHead>
              <TableHead>Ответ</TableHead>
              <TableHead>Следующая попытка</TableHead>
              <TableHead>Ошибка</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => (
              <TableRow key={item.id}>
                <TableCell className="whitespace-nowrap">
                  {new Date(item.created_at).toLocaleString("ru-RU")}
                </TableCell>
                <TableCell>{item.event_id.slice(0, 8)}…</TableCell>
                <TableCell>
                  <Status value={item.status} />
                  <p>попытка {item.attempt}</p>
                </TableCell>
                <TableCell>
                  {item.response_code ?? "—"} · {item.response_ms ?? "—"} мс
                </TableCell>
                <TableCell className="whitespace-nowrap">
                  {new Date(item.next_attempt_at).toLocaleString("ru-RU")}
                </TableCell>
                <TableCell>{item.error_summary ?? "—"}</TableCell>
                <TableCell>
                  {item.status === "failed" || item.status === "dead" ? (
                    <RetryDeliveryButton
                      deliveryId={item.id}
                      deliveryKind="webhook"
                      spaceSlug={spaceSlug}
                    />
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <JournalPager
          hasPrevious={hasPrevious}
          nextCursor={nextCursor}
          onNext={onNext}
        />
      </CardContent>
    </Card>
  );
}

export function DeliveryCenterPage() {
  const { spaceSlug } = useParams({
    from: "/_authenticated/spaces/$spaceSlug/delivery-center",
  });
  const search = useSearch({
    from: "/_authenticated/spaces/$spaceSlug/delivery-center",
  });
  const navigate = useNavigate({ from: "/spaces/$spaceSlug/delivery-center" });
  const query = {
    webhook_limit: 50,
    notification_limit: 50,
    ...(search.webhookCursor === undefined
      ? {}
      : { webhook_cursor: search.webhookCursor }),
    ...(search.notificationCursor === undefined
      ? {}
      : { notification_cursor: search.notificationCursor }),
  };
  const center = useQuery({
    queryKey: ["space", spaceSlug, "delivery-center", query],
    queryFn: ({ signal }) => fetchDeliveryCenter(spaceSlug, query, signal),
    placeholderData: (previous) => previous,
    refetchInterval: POLL_INTERVAL.backgroundJobs,
  });
  const principal = useQuery({
    queryKey: ["auth", "principal"],
    queryFn: ({ signal }) => fetchPrincipal(signal),
  });
  const canAct = principal.data?.role !== "viewer";
  const isAdmin = principal.data?.role === "admin";
  const patch = (value: Record<string, string | undefined>) => {
    void navigate({ search: (previous) => ({ ...previous, ...value }) });
  };
  const tab = search.tab ?? "telegram";
  return (
    <ListPage
      eyebrow="Пространство"
      title={`Уведомления и вебхуки · ${spaceSlug}`}
      description="Безопасные маски получателей, предпросмотр правил и журналы доставок. Секреты и полные webhook URL не передаются в браузер."
    >
      {canAct ? (
        <IntegrationCreateControls isAdmin={isAdmin} spaceSlug={spaceSlug} />
      ) : null}
      <QueryState
        query={center}
        skeleton={<Skeleton className="h-96 w-full" />}
        empty={{
          title: "Интеграции не найдены",
          description: "Добавьте Telegram-канал или webhook endpoint.",
        }}
        isEmpty={(data) =>
          data.channels.length === 0 && data.webhook_endpoints.length === 0
        }
      >
        {(data) => (
          <Tabs
            value={tab}
            onValueChange={(value) => {
              patch({ tab: value });
            }}
          >
            <TabsList aria-label="Разделы центра доставок">
              <TabsTrigger value="telegram">Telegram и правила</TabsTrigger>
              <TabsTrigger value="notifications">Уведомления</TabsTrigger>
              <TabsTrigger value="webhooks">Вебхуки</TabsTrigger>
            </TabsList>
            <TabsContent value="telegram">
              <TelegramTab
                data={data}
                isAdmin={isAdmin}
                spaceSlug={spaceSlug}
              />
            </TabsContent>
            <TabsContent value="notifications">
              <NotificationJournal
                items={data.notifications}
                nextCursor={data.notification_next_cursor}
                hasPrevious={search.notificationCursor !== undefined}
                onNext={(cursor) => {
                  patch({ notificationCursor: cursor });
                }}
                spaceSlug={spaceSlug}
              />
            </TabsContent>
            <TabsContent value="webhooks">
              <WebhooksTab
                data={data}
                hasPrevious={search.webhookCursor !== undefined}
                isAdmin={isAdmin}
                onNext={(cursor) => {
                  patch({ webhookCursor: cursor });
                }}
                spaceSlug={spaceSlug}
              />
            </TabsContent>
          </Tabs>
        )}
      </QueryState>
    </ListPage>
  );
}
