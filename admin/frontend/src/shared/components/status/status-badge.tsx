import { AlertTriangle } from "lucide-react";
import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { Badge } from "@/shared/ui/badge";

const statusBadgeVariants = cva("gap-1 font-medium", {
  variants: {
    tone: {
      neutral: "border-border bg-muted text-muted-foreground",
      info: "bg-info text-info-foreground border-transparent",
      success: "bg-success text-success-foreground border-transparent",
      warning: "bg-warning text-warning-foreground border-transparent",
      danger: "bg-destructive text-destructive-foreground border-transparent",
    },
  },
  defaultVariants: { tone: "neutral" },
});

export type StatusTone = NonNullable<
  VariantProps<typeof statusBadgeVariants>["tone"]
>;

type StatusBadgeProps = Omit<
  ComponentProps<typeof Badge>,
  "className" | "variant"
> & {
  tone: StatusTone;
  unknown?: boolean;
};

export function StatusBadge({
  tone,
  unknown = false,
  children,
  ...props
}: StatusBadgeProps) {
  return (
    <Badge
      variant="outline"
      className={statusBadgeVariants({ tone })}
      {...props}
    >
      {unknown ? <AlertTriangle aria-hidden="true" /> : null}
      {children}
    </Badge>
  );
}
