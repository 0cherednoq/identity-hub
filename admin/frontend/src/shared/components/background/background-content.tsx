import type { BackgroundSnapshotResponse } from "@/shared/api/generated/types.gen";
import { Badge } from "@/shared/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

function Status({ value }: { value: string }) {
  return (
    <Badge
      variant={
        value === "healthy"
          ? "secondary"
          : value === "retrying"
            ? "destructive"
            : "outline"
      }
    >
      {value}
    </Badge>
  );
}

export function BackgroundContent({
  data,
}: {
  data: BackgroundSnapshotResponse;
}) {
  const counts = Object.fromEntries(
    ["healthy", "delayed", "retrying", "stopped"].map((status) => [
      status,
      data.processes.filter((item) => item.status === status).length,
    ]),
  );
  return (
    <div className="grid gap-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {Object.entries(counts).map(([status, count]) => (
          <Card key={status}>
            <CardHeader>
              <CardTitle className="flex items-center justify-between">
                <Status value={status} /> <span>{count}</span>
              </CardTitle>
            </CardHeader>
          </Card>
        ))}
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Процессы · {data.processes.length}</CardTitle>
          <p className="text-muted-foreground text-sm">
            Обновлено {new Date(data.generated_at).toLocaleString("ru-RU")}
          </p>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Процесс</TableHead>
                <TableHead>Состояние</TableHead>
                <TableHead>Heartbeat / успех</TableHead>
                <TableHead>Lag / backlog</TableHead>
                <TableHead>Retry</TableHead>
                <TableHead>Диагностика</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.processes.map((item) => (
                <TableRow key={item.name}>
                  <TableCell>
                    <p className="font-medium">{item.name}</p>
                    <p className="text-muted-foreground text-xs">{item.kind}</p>
                  </TableCell>
                  <TableCell>
                    <Status value={item.status} />
                  </TableCell>
                  <TableCell>
                    {item.heartbeat_available ? (
                      <>
                        <p>
                          {item.last_attempt_at === null
                            ? "—"
                            : new Date(item.last_attempt_at).toLocaleString(
                                "ru-RU",
                              )}
                        </p>
                        <p className="text-muted-foreground text-xs">
                          успех:{" "}
                          {item.last_success_at === null
                            ? "—"
                            : new Date(item.last_success_at).toLocaleString(
                                "ru-RU",
                              )}
                        </p>
                      </>
                    ) : (
                      <Badge variant="outline">Heartbeat недоступен</Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <p>
                      events: {item.lag_events ?? "—"} ·{" "}
                      {item.lag_seconds ?? "—"}с
                    </p>
                    <p>
                      queue: {item.backlog ?? "—"} · failed:{" "}
                      {item.failed ?? "—"}
                    </p>
                  </TableCell>
                  <TableCell>
                    {item.next_attempt_at === null
                      ? "—"
                      : new Date(item.next_attempt_at).toLocaleString("ru-RU")}
                    {item.last_error === null ? null : (
                      <p className="text-destructive text-xs">
                        {item.last_error}
                      </p>
                    )}
                  </TableCell>
                  <TableCell className="max-w-80 text-sm">
                    {item.runbook_hint}
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
