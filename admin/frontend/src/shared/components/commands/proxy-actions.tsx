import { useState } from "react";

import { executeProxyAction } from "@/shared/api/proxy-commands";
import type {
  ProxyActionResponse,
  ResourceCommandResponse,
} from "@/shared/api/generated/types.gen";
import { useAdminCommand } from "@/shared/api/commands";
import { AdminApiError } from "@/shared/api/transport";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";

export function ProxyActions({
  actions,
  proxyId,
  spaceSlug,
}: {
  actions: ProxyActionResponse[];
  proxyId: string;
  spaceSlug: string;
}) {
  const [active, setActive] = useState<ProxyActionResponse | null>(null);
  const [reason, setReason] = useState("");
  const [target, setTarget] = useState("");
  const [pinHours, setPinHours] = useState("0");
  const [confirmed, setConfirmed] = useState(false);
  const command = useAdminCommand<
    Parameters<typeof executeProxyAction>[0],
    ResourceCommandResponse
  >({
    mutationFn: executeProxyAction,
    affectedQueries: () => [
      ["space", spaceSlug, "proxy", proxyId],
      ["space", spaceSlug, "proxies"],
      ["space", spaceSlug, "accounts"],
    ],
  });
  if (actions.length === 0) return null;

  const choose = (action: ProxyActionResponse) => {
    setActive(action);
    setTarget(action.allowed_targets?.[0] ?? "");
    setConfirmed(!action.requires_confirmation);
    command.reset();
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Действия с прокси</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="flex flex-wrap gap-2">
          {actions.map((action) => (
            <Button
              key={action.id}
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                choose(action);
              }}
            >
              {action.label}
            </Button>
          ))}
        </div>
        {active === null ? null : (
          <section
            className="bg-muted/40 grid gap-4 rounded-md border p-4"
            aria-label={active.label}
          >
            <h3 className="font-medium">{active.label}</h3>
            <div className="flex flex-wrap gap-2">
              {(active.consequence_codes ?? []).map((code) => (
                <Badge key={code} variant="outline">
                  {code.replaceAll("_", " ")}
                </Badge>
              ))}
            </div>
            {active.kind === "override" ? (
              <>
                <label className="grid gap-1 text-sm">
                  Статус
                  <select
                    className="border-input bg-background h-9 rounded-md border px-3"
                    value={target}
                    onChange={(event) => {
                      setTarget(event.target.value);
                    }}
                  >
                    {(active.allowed_targets ?? []).map((value) => (
                      <option key={value}>{value}</option>
                    ))}
                  </select>
                </label>
                <label className="grid gap-1 text-sm">
                  Закрепить на часов (0 — без закрепления)
                  <Input
                    type="number"
                    min="0"
                    max="720"
                    value={pinHours}
                    onChange={(event) => {
                      setPinHours(event.target.value);
                    }}
                  />
                </label>
              </>
            ) : null}
            <label className="grid gap-1 text-sm">
              Причина
              <textarea
                className="border-input bg-background min-h-24 rounded-md border px-3 py-2"
                maxLength={500}
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                }}
              />
            </label>
            {active.requires_confirmation ? (
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(event) => {
                    setConfirmed(event.target.checked);
                  }}
                />
                Я понимаю последствия ручного изменения статуса
              </label>
            ) : null}
            {command.error instanceof AdminApiError ? (
              <p className="text-destructive text-sm" role="alert">
                {command.error.message}
              </p>
            ) : null}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setActive(null);
                  command.reset();
                }}
              >
                Отмена
              </Button>
              <Button
                type="button"
                disabled={
                  command.isPending ||
                  reason.trim().length < 3 ||
                  !confirmed ||
                  (active.kind === "override" && target.length === 0)
                }
                onClick={() => {
                  const hours = Number(pinHours);
                  command.mutate(
                    {
                      action: active,
                      proxyId,
                      reason,
                      spaceSlug,
                      ...(active.kind === "override" ? { target } : {}),
                      ...(hours > 0 ? { pinSeconds: hours * 3600 } : {}),
                    },
                    {
                      onSuccess: () => {
                        setActive(null);
                        setReason("");
                        command.reset();
                      },
                    },
                  );
                }}
              >
                {command.isPending ? "Выполняется…" : "Выполнить"}
              </Button>
            </div>
          </section>
        )}
      </CardContent>
    </Card>
  );
}
