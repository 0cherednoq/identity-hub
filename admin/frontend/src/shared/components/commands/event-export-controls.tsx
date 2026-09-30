import { useState } from "react";

import { downloadExport } from "@/shared/api/bulk";
import type { EventExportFilter } from "@/shared/api/generated/types.gen";
import { AdminApiError } from "@/shared/api/transport";
import { Button } from "@/shared/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";

export function EventExportControls({
  filter,
  spaceSlug,
}: {
  filter: EventExportFilter;
  spaceSlug: string;
}) {
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  const run = async (format: "csv" | "json") => {
    setPending(true);
    setError("");
    try {
      await downloadExport({
        spaceSlug,
        command: {
          source: "events",
          format,
          reason,
          event_filter: filter,
        },
      });
    } catch (cause) {
      setError(
        cause instanceof AdminApiError || cause instanceof Error
          ? cause.message
          : "Экспорт отклонён.",
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Экспорт текущего фильтра</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-wrap items-end gap-3">
        <label className="grid min-w-64 flex-1 gap-1 text-sm">
          Причина
          <Input
            value={reason}
            maxLength={500}
            onChange={(event) => {
              setReason(event.target.value);
            }}
          />
        </label>
        <Button
          type="button"
          variant="outline"
          disabled={pending || reason.trim().length < 3}
          onClick={() => {
            void run("csv");
          }}
        >
          Экспорт CSV
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={pending || reason.trim().length < 3}
          onClick={() => {
            void run("json");
          }}
        >
          Экспорт JSON
        </Button>
        {error ? (
          <p className="text-destructive w-full text-sm" role="alert">
            {error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
