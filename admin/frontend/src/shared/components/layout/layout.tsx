import type { ReactNode } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
} from "@/shared/ui/card";

export function PageContainer({ children }: { children: ReactNode }) {
  return (
    <main className="mx-auto grid w-full max-w-7xl gap-6 px-4 py-6 sm:px-6 lg:px-8">
      {children}
    </main>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-col gap-4 border-b pb-6 sm:flex-row sm:items-end sm:justify-between">
      <div className="grid min-w-0 gap-1">
        {eyebrow === undefined ? null : (
          <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
            {eyebrow}
          </p>
        )}
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description === undefined ? null : (
          <p className="text-muted-foreground max-w-3xl text-sm">
            {description}
          </p>
        )}
      </div>
      {actions === undefined ? null : (
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      )}
    </header>
  );
}

export function Section({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between">
        <div className="grid gap-1">
          <h2 className="leading-none font-semibold">{title}</h2>
          {description === undefined ? null : (
            <CardDescription>{description}</CardDescription>
          )}
        </div>
        {actions}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}
