import type { ReactNode } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/ui/card";

export function MetricGrid({ children }: { children: ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {children}
    </div>
  );
}

export function MetricCard({
  label,
  value,
  description,
}: {
  label: string;
  value: number;
  description?: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="font-mono text-2xl tabular-nums">
          {new Intl.NumberFormat("ru-RU").format(value)}
        </CardTitle>
      </CardHeader>
      {description === undefined ? null : (
        <CardContent>
          <p className="text-muted-foreground text-xs">{description}</p>
        </CardContent>
      )}
    </Card>
  );
}
