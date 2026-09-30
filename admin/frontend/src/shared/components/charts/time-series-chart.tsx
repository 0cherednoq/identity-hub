import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/shared/ui/chart";

export interface TimeSeriesPoint {
  label: string;
  value: number;
}

export interface DualTimeSeriesPoint {
  label: string;
  primary: number;
  secondary: number;
}

const chartConfig = {
  value: { label: "Значение", color: "var(--color-chart-1)" },
} satisfies ChartConfig;

export function TimeSeriesChart({ data }: { data: TimeSeriesPoint[] }) {
  return (
    <ChartContainer className="h-64 w-full" config={chartConfig}>
      <LineChart accessibilityLayer data={data} margin={{ left: 8, right: 8 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="label" tickLine={false} axisLine={false} />
        <YAxis width={36} tickLine={false} axisLine={false} />
        <ChartTooltip cursor={false} content={<ChartTooltipContent />} />
        <Line
          dataKey="value"
          type="monotone"
          stroke="var(--color-value)"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartContainer>
  );
}

const dualChartConfig = {
  primary: { label: "Взято", color: "var(--color-chart-1)" },
  secondary: { label: "Lost + expired", color: "var(--color-chart-2)" },
} satisfies ChartConfig;

export function DualTimeSeriesChart({ data }: { data: DualTimeSeriesPoint[] }) {
  return (
    <ChartContainer className="h-72 w-full" config={dualChartConfig}>
      <LineChart accessibilityLayer data={data} margin={{ left: 8, right: 8 }}>
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey="label"
          tickLine={false}
          axisLine={false}
          minTickGap={24}
        />
        <YAxis
          width={36}
          allowDecimals={false}
          tickLine={false}
          axisLine={false}
        />
        <ChartTooltip content={<ChartTooltipContent />} />
        <Line
          dataKey="primary"
          type="monotone"
          stroke="var(--color-primary)"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          dataKey="secondary"
          type="monotone"
          stroke="var(--color-secondary)"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartContainer>
  );
}
