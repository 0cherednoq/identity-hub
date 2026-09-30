import { useEffect, useState } from "react";

import { useAdminCommand } from "@/shared/api/commands";
import {
  executeIntegrationCommand,
  type IntegrationCommand,
  type IntegrationCommandResult,
} from "@/shared/api/integrations";
import { AdminApiError } from "@/shared/api/transport";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";

function errorMessage(error: unknown): string {
  return error instanceof AdminApiError || error instanceof Error
    ? error.message
    : "Команда отклонена.";
}

function eventTypes(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function filterValue(value: string): Record<string, unknown> {
  if (value.trim() === "") return {};
  const parsed: unknown = JSON.parse(value);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("Фильтр должен быть JSON-объектом.");
  }
  return parsed as Record<string, unknown>;
}

function useIntegrationCommand(spaceSlug: string, sensitive = false) {
  return useAdminCommand<IntegrationCommand, IntegrationCommandResult>({
    mutationFn: executeIntegrationCommand,
    affectedQueries: () => [
      ["space", spaceSlug, "delivery-center"],
      ["space", spaceSlug, "events"],
    ],
    sensitive,
  });
}

export function IntegrationCreateControls({
  isAdmin,
  spaceSlug,
}: {
  isAdmin: boolean;
  spaceSlug: string;
}) {
  const [kind, setKind] = useState<"channel" | "rule" | "webhook">(
    isAdmin ? "channel" : "rule",
  );
  const [reason, setReason] = useState("");
  const [title, setTitle] = useState("");
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [channelId, setChannelId] = useState("");
  const [name, setName] = useState("");
  const [patterns, setPatterns] = useState("identity.*");
  const [filter, setFilter] = useState("{}");
  const [template, setTemplate] = useState("");
  const [digest, setDigest] = useState("");
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [localError, setLocalError] = useState("");
  const [oneTimeValue, setOneTimeValue] = useState("");
  const command = useIntegrationCommand(spaceSlug, kind !== "rule");

  useEffect(() => {
    if (oneTimeValue === "") return;
    const clear = () => {
      setOneTimeValue("");
    };
    const onVisibility = () => {
      if (document.hidden) clear();
    };
    const timer = window.setTimeout(clear, 60_000);
    window.addEventListener("blur", clear);
    window.addEventListener("pagehide", clear);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("blur", clear);
      window.removeEventListener("pagehide", clear);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [oneTimeValue]);

  const submit = () => {
    setLocalError("");
    setOneTimeValue("");
    try {
      let value: IntegrationCommand;
      if (kind === "channel") {
        value = {
          body: {
            bot_token: token,
            chat_id: chatId,
            title,
          },
          kind: "channel.create",
          reason,
          spaceSlug,
        };
      } else if (kind === "rule") {
        const seconds = digest.trim() === "" ? null : Number(digest);
        value = {
          body: {
            channel_id: channelId,
            digest_seconds: seconds,
            event_types: eventTypes(patterns),
            filter: filterValue(filter),
            name,
            ...(template.trim() === "" ? {} : { template }),
          },
          kind: "rule.create",
          reason,
          spaceSlug,
        };
      } else {
        value = {
          body: {
            description: description.trim() === "" ? null : description,
            event_types: eventTypes(patterns),
            filter: filterValue(filter),
            url,
          },
          kind: "webhook.create",
          reason,
          spaceSlug,
        };
      }
      command.mutate(value, {
        onSuccess: (result) => {
          setToken("");
          if ("one_time_signing_value" in result) {
            setOneTimeValue(result.one_time_signing_value);
          }
        },
      });
    } catch (error) {
      setLocalError(errorMessage(error));
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Добавить интеграцию</CardTitle>
        <p className="text-muted-foreground text-sm">
          Токены не возвращаются API. Ключ подписи webhook показывается только в
          ответе на создание и очищается через 60 секунд.
        </p>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="flex flex-wrap gap-2">
          {isAdmin ? (
            <Button
              type="button"
              size="sm"
              variant={kind === "channel" ? "default" : "outline"}
              onClick={() => {
                setKind("channel");
              }}
            >
              Telegram
            </Button>
          ) : null}
          <Button
            type="button"
            size="sm"
            variant={kind === "rule" ? "default" : "outline"}
            onClick={() => {
              setKind("rule");
            }}
          >
            Правило
          </Button>
          {isAdmin ? (
            <Button
              type="button"
              size="sm"
              variant={kind === "webhook" ? "default" : "outline"}
              onClick={() => {
                setKind("webhook");
              }}
            >
              Webhook
            </Button>
          ) : null}
        </div>
        {kind === "channel" ? (
          <div className="grid gap-3 md:grid-cols-3">
            <Field label="Название" value={title} onChange={setTitle} />
            <Field label="Chat ID" value={chatId} onChange={setChatId} />
            <Field
              label="Bot token"
              value={token}
              onChange={setToken}
              type="password"
            />
          </div>
        ) : null}
        {kind === "rule" ? (
          <div className="grid gap-3 md:grid-cols-2">
            <Field label="Название" value={name} onChange={setName} />
            <Field
              label="ID канала"
              value={channelId}
              onChange={setChannelId}
            />
            <Field
              label="Типы событий через запятую"
              value={patterns}
              onChange={setPatterns}
            />
            <Field
              label="Дайджест, секунд (пусто — сразу)"
              value={digest}
              onChange={setDigest}
              type="number"
            />
            <Field label="Фильтр JSON" value={filter} onChange={setFilter} />
            <Field
              label="Шаблон (необязательно)"
              value={template}
              onChange={setTemplate}
            />
          </div>
        ) : null}
        {kind === "webhook" ? (
          <div className="grid gap-3 md:grid-cols-2">
            <Field label="HTTPS URL" value={url} onChange={setUrl} />
            <Field
              label="Описание"
              value={description}
              onChange={setDescription}
            />
            <Field
              label="Типы событий через запятую"
              value={patterns}
              onChange={setPatterns}
            />
            <Field label="Фильтр JSON" value={filter} onChange={setFilter} />
          </div>
        ) : null}
        <label className="grid gap-1 text-sm">
          Причина
          <textarea
            className="border-input bg-background min-h-20 rounded-md border px-3 py-2"
            maxLength={500}
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
            }}
          />
        </label>
        {oneTimeValue === "" ? null : (
          <div className="border-warning bg-warning/10 grid gap-2 rounded-md border p-3">
            <strong>Сохраните ключ подписи сейчас</strong>
            <code className="break-all select-all">{oneTimeValue}</code>
            <p className="text-sm">
              После закрытия блока ключ нельзя получить снова.
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                setOneTimeValue("");
              }}
            >
              Я сохранил ключ
            </Button>
          </div>
        )}
        {localError !== "" || command.error !== null ? (
          <p className="text-destructive text-sm" role="alert">
            {localError || errorMessage(command.error)}
          </p>
        ) : null}
        <div className="flex justify-end">
          <Button
            type="button"
            disabled={command.isPending || reason.trim().length < 3}
            onClick={submit}
          >
            {command.isPending ? "Сохраняется…" : "Создать"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function Field({
  label,
  onChange,
  type = "text",
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  type?: string;
  value: string;
}) {
  return (
    <label className="grid gap-1 text-sm">
      {label}
      <Input
        type={type}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
    </label>
  );
}

type RowKind = "channel" | "rule" | "webhook";
type RowAction = "delete" | "rotate" | "test" | "toggle";

export function IntegrationRowActions({
  enabled,
  id,
  isAdmin,
  kind,
  spaceSlug,
}: {
  enabled: boolean;
  id: string;
  isAdmin: boolean;
  kind: RowKind;
  spaceSlug: string;
}) {
  const [active, setActive] = useState<RowAction | null>(null);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [message, setMessage] = useState("Identity Hub: тестовое уведомление");
  const [overlap, setOverlap] = useState("3600");
  const [oneTimeValue, setOneTimeValue] = useState("");
  const command = useIntegrationCommand(spaceSlug, active === "rotate");

  useEffect(() => {
    if (oneTimeValue === "") return;
    const clear = () => {
      setOneTimeValue("");
    };
    const timer = window.setTimeout(clear, 60_000);
    window.addEventListener("blur", clear);
    window.addEventListener("pagehide", clear);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("blur", clear);
      window.removeEventListener("pagehide", clear);
    };
  }, [oneTimeValue]);

  const choose = (value: RowAction) => {
    setActive(value);
    setConfirmed(value !== "delete" && value !== "rotate");
    setOneTimeValue("");
    command.reset();
  };
  const submit = () => {
    let value: IntegrationCommand;
    if (kind === "channel") {
      value =
        active === "test"
          ? {
              channelId: id,
              kind: "channel.test",
              reason,
              spaceSlug,
              text: message,
            }
          : active === "delete"
            ? { channelId: id, kind: "channel.delete", reason, spaceSlug }
            : {
                channelId: id,
                enabled: !enabled,
                kind: "channel.enable",
                reason,
                spaceSlug,
              };
    } else if (kind === "rule") {
      value =
        active === "delete"
          ? { kind: "rule.delete", reason, ruleId: id, spaceSlug }
          : {
              enabled: !enabled,
              kind: "rule.enable",
              reason,
              ruleId: id,
              spaceSlug,
            };
    } else if (active === "test") {
      value = { endpointId: id, kind: "webhook.test", reason, spaceSlug };
    } else if (active === "rotate") {
      value = {
        endpointId: id,
        kind: "webhook.rotate",
        overlapSeconds: Number(overlap),
        reason,
        spaceSlug,
      };
    } else if (active === "delete") {
      value = { endpointId: id, kind: "webhook.delete", reason, spaceSlug };
    } else {
      value = {
        enabled: !enabled,
        endpointId: id,
        kind: "webhook.enable",
        reason,
        spaceSlug,
      };
    }
    command.mutate(value, {
      onSuccess: (result) => {
        if ("one_time_signing_value" in result) {
          setOneTimeValue(result.one_time_signing_value);
        } else {
          setActive(null);
          setReason("");
        }
      },
    });
  };

  return (
    <div className="grid min-w-56 gap-2">
      <div className="flex flex-wrap gap-1">
        {kind === "rule" ? null : (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              choose("test");
            }}
          >
            Тест
          </Button>
        )}
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => {
            choose("toggle");
          }}
        >
          {enabled ? "Отключить" : "Включить"}
        </Button>
        {kind === "webhook" && isAdmin ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              choose("rotate");
            }}
          >
            Ротация
          </Button>
        ) : null}
        {kind === "rule" || isAdmin ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              choose("delete");
            }}
          >
            Удалить
          </Button>
        ) : null}
      </div>
      {active === null ? null : (
        <div className="bg-muted/40 grid gap-2 rounded-md border p-3">
          <Badge variant="outline">{active}</Badge>
          {active === "test" && kind === "channel" ? (
            <Field label="Текст теста" value={message} onChange={setMessage} />
          ) : null}
          {active === "rotate" ? (
            <Field
              label="Перекрытие ключей, секунд"
              value={overlap}
              onChange={setOverlap}
              type="number"
            />
          ) : null}
          <Field label="Причина" value={reason} onChange={setReason} />
          {active === "delete" || active === "rotate" ? (
            <label className="flex gap-2 text-sm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => {
                  setConfirmed(event.target.checked);
                }}
              />
              Я понимаю последствия
            </label>
          ) : null}
          {oneTimeValue === "" ? null : (
            <code className="break-all select-all">{oneTimeValue}</code>
          )}
          {command.error === null ? null : (
            <p className="text-destructive text-sm" role="alert">
              {errorMessage(command.error)}
            </p>
          )}
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                setActive(null);
                setOneTimeValue("");
              }}
            >
              Закрыть
            </Button>
            {oneTimeValue === "" ? (
              <Button
                type="button"
                size="sm"
                disabled={
                  command.isPending || reason.trim().length < 3 || !confirmed
                }
                onClick={submit}
              >
                {command.isPending ? "Выполняется…" : "Подтвердить"}
              </Button>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}

export function RetryDeliveryButton({
  deliveryKind,
  deliveryId,
  spaceSlug,
}: {
  deliveryKind: "notification" | "webhook";
  deliveryId: number;
  spaceSlug: string;
}) {
  const [confirming, setConfirming] = useState(false);
  const command = useIntegrationCommand(spaceSlug);
  return confirming ? (
    <div className="grid min-w-52 gap-2">
      <p className="text-sm">
        Retry сбрасывает попытки и снова включает endpoint.
      </p>
      {command.error === null ? null : (
        <p className="text-destructive text-sm">
          {errorMessage(command.error)}
        </p>
      )}
      <div className="flex gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => {
            setConfirming(false);
          }}
        >
          Отмена
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={command.isPending}
          onClick={() => {
            command.mutate(
              {
                deliveryId,
                kind:
                  deliveryKind === "webhook"
                    ? "delivery.retry"
                    : "notification-delivery.retry",
                reason: "Manual retry after delivery review",
                spaceSlug,
              },
              {
                onSuccess: () => {
                  setConfirming(false);
                },
              },
            );
          }}
        >
          {command.isPending ? "Ставится в очередь…" : "Повторить"}
        </Button>
      </div>
    </div>
  ) : (
    <Button
      type="button"
      size="sm"
      variant="outline"
      onClick={() => {
        setConfirming(true);
      }}
    >
      Retry
    </Button>
  );
}
