import { useState } from "react";

import type { AccountActionResponse } from "@/shared/api/generated/types.gen";
import {
  type AccountCommandResult,
  executeAccountAction,
  type AccountActionVariables,
} from "@/shared/api/account-commands";
import { useAdminCommand } from "@/shared/api/commands";
import { AdminApiError } from "@/shared/api/transport";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";

import { CommandConflictDialog } from "./command-conflict-dialog";

export function AccountActions({
  actions,
  identityId,
  revision,
  spaceSlug,
  onReload,
}: {
  actions: AccountActionResponse[];
  identityId: string;
  revision: number;
  spaceSlug: string;
  onReload: () => void;
}) {
  const available = actions;
  const [active, setActive] = useState<AccountActionResponse | null>(null);
  const [reason, setReason] = useState("");
  const [role, setRole] = useState("");
  const [answer, setAnswer] = useState("");
  const [target, setTarget] = useState("");
  const [pinHours, setPinHours] = useState("0");
  const [confirmed, setConfirmed] = useState(false);
  const command = useAdminCommand<AccountActionVariables, AccountCommandResult>(
    {
      mutationFn: executeAccountAction,
      sensitive: true,
      affectedQueries: ({ action }) => {
        const keys: Array<readonly unknown[]> = [
          ["space", spaceSlug, "account", identityId],
        ];
        if (["proxy_rebind", "link", "unlink"].includes(action.kind)) {
          keys.push(["space", spaceSlug, "proxies"]);
        }
        if (
          [
            "answer_challenge",
            "resolve_requirement",
            "verification_approve",
            "verification_reject",
            "verification_abandon",
          ].includes(action.kind)
        ) {
          keys.push(["space", spaceSlug, "human-queue"]);
        }
        if (
          [
            "lifecycle",
            "restore",
            "override",
            "unpin",
            "lease_revoke",
            "link",
            "unlink",
            "proxy_rebind",
          ].includes(action.kind)
        ) {
          keys.push(["space", spaceSlug, "accounts"]);
        }
        return keys;
      },
    },
  );

  if (available.length === 0) return null;

  const selectAction = (action: AccountActionResponse) => {
    setActive(action);
    setAnswer("");
    setRole("");
    setTarget(action.target ?? action.allowed_targets?.[0] ?? "");
    setConfirmed(!action.requires_confirmation);
    command.reset();
  };
  const submit = () => {
    if (active === null) return;
    const hours = Number(pinHours);
    command.mutate(
      {
        action: active,
        identityId,
        reason,
        ...(active.kind === "link" ? { role } : {}),
        revision,
        spaceSlug,
        ...(active.kind === "override" ? { target } : {}),
        ...(active.kind === "verification_reject" ? { target } : {}),
        ...(["link", "proxy_rebind"].includes(active.kind) ? { target } : {}),
        ...(active.kind === "answer_challenge" ? { answer } : {}),
        ...(active.pin_supported && hours > 0
          ? { pinSeconds: hours * 3600 }
          : {}),
      },
      {
        onSuccess: () => {
          setActive(null);
          setReason("");
          setAnswer("");
          setRole("");
          setConfirmed(false);
          command.reset();
        },
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Действия</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="flex flex-wrap gap-2">
          {available.map((action) => (
            <Button
              key={action.id}
              type="button"
              size="sm"
              variant={action.kind === "restore" ? "default" : "outline"}
              onClick={() => {
                selectAction(action);
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
            <div>
              <h3 className="font-medium">{active.label}</h3>
              <div className="mt-2 flex flex-wrap gap-2">
                {(active.consequence_codes ?? []).map((code) => (
                  <Badge key={code} variant="outline">
                    {code.replaceAll("_", " ")}
                  </Badge>
                ))}
              </div>
            </div>
            {active.kind === "override" ||
            active.kind === "verification_reject" ? (
              <label className="grid gap-1 text-sm">
                {active.kind === "override" ? "Новое состояние" : "Код отказа"}
                {(active.allowed_targets ?? []).length > 0 ? (
                  <select
                    className="border-input bg-background h-9 rounded-md border px-3"
                    value={target}
                    onChange={(event) => {
                      setTarget(event.target.value);
                    }}
                  >
                    {(active.allowed_targets ?? []).map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                ) : (
                  <Input
                    value={target}
                    maxLength={128}
                    onChange={(event) => {
                      setTarget(event.target.value);
                    }}
                  />
                )}
              </label>
            ) : null}
            {active.kind === "answer_challenge" ? (
              <label className="grid gap-1 text-sm">
                Ответ
                <Input
                  type="password"
                  autoComplete="one-time-code"
                  value={answer}
                  maxLength={4096}
                  onChange={(event) => {
                    setAnswer(event.target.value);
                  }}
                />
                <span className="text-muted-foreground text-xs">
                  Значение шифруется и удаляется из формы сразу после запроса.
                </span>
              </label>
            ) : null}
            {active.requires_role_input ? (
              <label className="grid gap-1 text-sm">
                Роль связи
                <Input
                  value={role}
                  maxLength={64}
                  placeholder="recovery_email"
                  onChange={(event) => {
                    setRole(event.target.value);
                  }}
                />
              </label>
            ) : null}
            {active.kind === "link" || active.kind === "proxy_rebind" ? (
              <label className="grid gap-1 text-sm">
                {active.kind === "link" ? "UUID связанного аккаунта" : "Прокси"}
                {(active.allowed_targets ?? []).length > 0 ? (
                  <select
                    className="border-input bg-background h-9 rounded-md border px-3"
                    value={target}
                    onChange={(event) => {
                      setTarget(event.target.value);
                    }}
                  >
                    {(active.allowed_targets ?? []).map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                ) : (
                  <Input
                    value={target}
                    onChange={(event) => {
                      setTarget(event.target.value);
                    }}
                  />
                )}
              </label>
            ) : null}
            {active.pin_supported ? (
              <label className="grid gap-1 text-sm">
                Закрепить на часов (0 — не закреплять)
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
                Я понимаю перечисленные последствия
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
                  setAnswer("");
                  setRole("");
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
                  ((active.kind === "override" ||
                    active.kind === "verification_reject") &&
                    target.length === 0) ||
                  (active.kind === "answer_challenge" && answer.length === 0) ||
                  (active.kind === "link" &&
                    (role.length === 0 || target.length === 0)) ||
                  (active.kind === "proxy_rebind" && target.length === 0)
                }
                onClick={submit}
              >
                {command.isPending ? "Выполняется…" : "Выполнить"}
              </Button>
            </div>
          </section>
        )}
      </CardContent>
      <CommandConflictDialog
        error={command.error}
        reason={reason}
        onClose={command.reset}
        onReload={() => {
          command.reset();
          onReload();
        }}
      />
    </Card>
  );
}
