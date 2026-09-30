import { isRevisionConflict } from "@/shared/api/commands";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";

export function CommandConflictDialog({
  error,
  reason,
  onClose,
  onReload,
}: {
  error: unknown;
  reason: string;
  onClose: () => void;
  onReload: () => void;
}) {
  if (!isRevisionConflict(error)) return null;
  const details = error.body?.error.details;
  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="revision-conflict-title"
      className="bg-background/80 fixed inset-0 z-50 grid place-items-center p-4"
    >
      <Card className="w-full max-w-lg">
        <CardHeader>
          <CardTitle id="revision-conflict-title">
            Данные уже изменились
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4">
          <p>
            Ожидалась версия {String(details?.expected_revision ?? "—")},
            текущая — {String(details?.current_revision ?? "—")}.
          </p>
          <p>Введённая причина сохранена: {reason}</p>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={onClose}>
              Закрыть
            </Button>
            <Button type="button" onClick={onReload}>
              Обновить данные
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
