import { useMemo, useRef } from "react";
import { Download, ImageDown } from "lucide-react";

import type { AnalyticsSeriesResponse } from "@/shared/api/generated/types.gen";
import { AnalyticsSeriesChart } from "@/shared/components/charts/analytics-series-chart";
import { analyticsChartRows } from "@/shared/lib/analytics-chart-data";
import type { ChartConfig } from "@/shared/ui/chart";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/ui/card";

const COLORS = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
];

function save(name: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}

function csv(series: AnalyticsSeriesResponse): string {
  const quote = (value: string | number) =>
    `"${String(value).replaceAll('"', '""')}"`;
  const headers = ["at", ...series.groups.map((group) => group.key)];
  const rows = Array.from(
    {
      length: Math.max(0, ...series.groups.map((group) => group.points.length)),
    },
    (_, index) => [
      series.groups[0]?.points[index]?.at ?? "",
      ...series.groups.map((group) => group.points[index]?.value ?? 0),
    ],
  );
  return [headers, ...rows].map((row) => row.map(quote).join(",")).join("\n");
}

function change(total: number, previous: number): string {
  if (previous === 0) return total === 0 ? "0%" : "новые данные";
  const value = ((total - previous) / previous) * 100;
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`;
}

export function AnalyticsSeriesCard({
  series,
  title,
  definition,
  timezone,
}: {
  series: AnalyticsSeriesResponse;
  title: string;
  definition: string;
  timezone: string;
}) {
  const chartRef = useRef<HTMLDivElement>(null);
  const rows = useMemo(
    () => analyticsChartRows(series, timezone),
    [series, timezone],
  );
  const config = useMemo(
    () =>
      Object.fromEntries(
        series.groups.map((group, index) => [
          `group${String(index)}`,
          {
            label: group.key || "Все",
            color: COLORS[index % COLORS.length] ?? "var(--color-chart-1)",
          },
        ]),
      ) satisfies ChartConfig,
    [series.groups],
  );
  const slug = series.metric.replaceAll(".", "-");

  const saveSvg = () => {
    const svg = chartRef.current?.querySelector("svg");
    if (svg === undefined || svg === null) return;
    const copy = svg.cloneNode(true) as SVGElement;
    copy.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    save(
      `${slug}.svg`,
      new XMLSerializer().serializeToString(copy),
      "image/svg+xml",
    );
  };

  return (
    <Card data-analytics-metric={series.metric}>
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="grid gap-1">
            <CardTitle>{title}</CardTitle>
            <CardDescription>{definition}</CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge
              variant={series.coverage === "complete" ? "secondary" : "outline"}
            >
              {series.coverage === "complete"
                ? "Полное покрытие"
                : "Неполное покрытие"}
            </Badge>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                save(`${slug}.csv`, csv(series), "text/csv;charset=utf-8");
              }}
            >
              <Download aria-hidden="true" /> CSV
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={saveSvg}>
              <ImageDown aria-hidden="true" /> SVG
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
          {series.groups.map((group) => (
            <span key={group.key} className="min-w-0">
              <strong className="font-mono tabular-nums">
                {group.total.toLocaleString("ru-RU")}
              </strong>{" "}
              <span className="text-muted-foreground break-all">
                {group.key || "Все"} ·{" "}
                {change(group.total, group.previous_total)} к предыдущему окну
              </span>
            </span>
          ))}
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div
          ref={chartRef}
          role="img"
          aria-label={`${title}. Временной ряд в зоне ${timezone}`}
        >
          <AnalyticsSeriesChart rows={rows} series={series} config={config} />
        </div>
        <details>
          <summary className="cursor-pointer text-sm font-medium">
            Таблица данных
          </summary>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-max text-sm">
              <thead>
                <tr className="border-b text-left">
                  <th className="p-2">Время ({timezone})</th>
                  {series.groups.map((group) => (
                    <th key={group.key} className="p-2 break-all">
                      {group.key || "Все"}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.at} className="border-b last:border-0">
                    <td className="p-2">{row.label}</td>
                    {series.groups.map((group, index) => (
                      <td
                        key={group.key}
                        className="p-2 font-mono tabular-nums"
                      >
                        {Number(row[`group${String(index)}`]).toLocaleString(
                          "ru-RU",
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </CardContent>
    </Card>
  );
}
