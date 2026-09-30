import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";

import type { AnalyticsSeriesResponse } from "@/shared/api/generated/types.gen";
import type { AnalyticsChartRow } from "@/shared/lib/analytics-chart-data";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/shared/ui/chart";

export function AnalyticsSeriesChart({
  rows,
  series,
  config,
}: {
  rows: AnalyticsChartRow[];
  series: AnalyticsSeriesResponse;
  config: ChartConfig;
}) {
  return (
    <ChartContainer className="h-72 w-full" config={config}>
      <LineChart accessibilityLayer data={rows} margin={{ left: 8, right: 8 }}>
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey="label"
          tickLine={false}
          axisLine={false}
          minTickGap={28}
        />
        <YAxis
          width={42}
          allowDecimals={false}
          tickLine={false}
          axisLine={false}
        />
        <ChartTooltip content={<ChartTooltipContent />} />
        <ChartLegend
          content={<ChartLegendContent className="flex-wrap justify-start" />}
        />
        {series.groups.map((group, index) => (
          <Line
            key={group.key}
            dataKey={`group${String(index)}`}
            type="monotone"
            stroke={`var(--color-group${String(index)})`}
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ChartContainer>
  );
}
