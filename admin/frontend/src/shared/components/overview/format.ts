export function formatPercent(value: number, total: number): string {
  if (total === 0) return "0%";
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 1,
    style: "percent",
  }).format(value / total);
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat("ru-RU").format(value);
}
