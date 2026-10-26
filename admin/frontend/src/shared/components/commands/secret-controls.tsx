import { useLayoutEffect, useState } from "react";

import type { SecretDescriptorResponse } from "@/shared/api/generated/types.gen";
import {
  revealAccountSecret,
  updateAccountSecret,
} from "@/shared/api/secret-commands";
import { AdminApiError } from "@/shared/api/transport";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";

type Mode = "reveal" | "update";

export function SecretControls({
  descriptors,
  identityId,
  revision,
  spaceSlug,
  onReload,
}: {
  descriptors: SecretDescriptorResponse[];
  identityId: string;
  revision: number;
  spaceSlug: string;
  onReload: () => void;
}) {
  const [active, setActive] = useState<SecretDescriptorResponse | null>(null);
  const [mode, setMode] = useState<Mode>("reveal");
  const [reason, setReason] = useState("");
  const [reauthentication, setReauthentication] = useState("");
  const [editor, setEditor] = useState("{}");
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [revealed, setRevealed] = useState<Record<string, unknown> | null>(
    null,
  );
  const [hideAfter, setHideAfter] = useState(0);

  const clearSensitive = () => {
    setReauthentication("");
    setEditor("{}");
    setRevealed(null);
    setHideAfter(0);
  };

  // Слушатели вешаются в том же коммите, что и показ значения: с useEffect подписка
  // запаздывала, и blur сразу после показа оставлял секрет на экране до таймера.
  useLayoutEffect(() => {
    if (revealed === null || hideAfter <= 0) return;
    const hide = () => {
      setRevealed(null);
      setHideAfter(0);
    };
    const timer = window.setTimeout(hide, hideAfter * 1000);
    const visibility = () => {
      if (document.hidden) hide();
    };
    window.addEventListener("blur", hide);
    window.addEventListener("pagehide", hide);
    window.addEventListener("popstate", hide);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("blur", hide);
      window.removeEventListener("pagehide", hide);
      window.removeEventListener("popstate", hide);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [hideAfter, revealed]);

  if (descriptors.length === 0) return null;

  const open = (descriptor: SecretDescriptorResponse, nextMode: Mode) => {
    clearSensitive();
    setActive(descriptor);
    setMode(nextMode);
    setReason("");
    setConfirmed(false);
    setError("");
  };

  const submit = async () => {
    if (active === null) return;
    setPending(true);
    setError("");
    try {
      if (mode === "reveal") {
        const response = await revealAccountSecret({
          field: active.field,
          identityId,
          reason,
          reauthentication,
          revision,
          spaceSlug,
        });
        setRevealed(response.value);
        setHideAfter(response.hide_after_seconds ?? 30);
      } else {
        if (active.update_kind === null || active.update_kind === undefined)
          return;
        const value: unknown = JSON.parse(editor);
        if (
          typeof value !== "object" ||
          value === null ||
          Array.isArray(value)
        ) {
          throw new Error("Введите JSON-объект.");
        }
        await updateAccountSecret({
          identityId,
          kind: active.update_kind,
          reason,
          reauthentication,
          revision,
          spaceSlug,
          ...(active.update_kind === "session"
            ? { slot: active.field.split(":", 2)[1] ?? "default" }
            : {}),
          value: value as Record<string, unknown>,
        });
        setActive(null);
      }
      setReauthentication("");
      onReload();
    } catch (cause) {
      setReauthentication("");
      setError(
        cause instanceof AdminApiError || cause instanceof Error
          ? cause.message
          : "Команда отклонена.",
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Секретные значения</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <p className="text-muted-foreground text-sm">
          Значения не попадают в query cache и скрываются через 30 секунд, при
          смене вкладки или потере фокуса.
        </p>
        <div className="grid gap-2">
          {descriptors.map((descriptor) => (
            <div
              key={descriptor.field}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3"
            >
              <span>{descriptor.label}</span>
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    open(descriptor, "reveal");
                  }}
                >
                  Показать
                </Button>
                {descriptor.update_kind === null ||
                descriptor.update_kind === undefined ? null : (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      open(descriptor, "update");
                    }}
                  >
                    Обновить
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
        {active === null ? null : (
          <section
            className="bg-muted/40 grid gap-4 rounded-md border p-4"
            aria-label={`${mode === "reveal" ? "Показать" : "Обновить"} ${active.label}`}
          >
            <h3 className="font-medium">{active.label}</h3>
            {mode === "update" ? (
              <label className="grid gap-1 text-sm">
                Новое значение (JSON)
                <textarea
                  className="border-input bg-background min-h-32 rounded-md border px-3 py-2 font-mono text-xs"
                  value={editor}
                  onChange={(event) => {
                    setEditor(event.target.value);
                  }}
                />
              </label>
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
            <label className="grid gap-1 text-sm">
              Пароль или текущий admin-токен
              <Input
                type="password"
                autoComplete="current-password"
                value={reauthentication}
                onChange={(event) => {
                  setReauthentication(event.target.value);
                }}
              />
            </label>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => {
                  setConfirmed(event.target.checked);
                }}
              />
              Я подтверждаю доступ к секретному значению
            </label>
            {error ? (
              <p className="text-destructive text-sm" role="alert">
                {error}
              </p>
            ) : null}
            {revealed === null ? null : (
              <div className="grid gap-2" aria-live="polite">
                <p className="text-sm">
                  Значение будет скрыто через {hideAfter} секунд.
                </p>
                <pre
                  data-testid="secret-value"
                  className="bg-background max-h-64 overflow-auto rounded-md border p-3 text-xs whitespace-pre-wrap"
                >
                  {JSON.stringify(revealed, null, 2)}
                </pre>
              </div>
            )}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  clearSensitive();
                  setActive(null);
                }}
              >
                Закрыть
              </Button>
              <Button
                type="button"
                disabled={
                  pending ||
                  reason.trim().length < 3 ||
                  reauthentication.length === 0 ||
                  !confirmed
                }
                onClick={() => {
                  void submit();
                }}
              >
                {pending
                  ? "Выполняется…"
                  : mode === "reveal"
                    ? "Показать значение"
                    : "Сохранить"}
              </Button>
            </div>
          </section>
        )}
      </CardContent>
    </Card>
  );
}
