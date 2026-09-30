import { Link } from "@tanstack/react-router";

import type {
  OverviewTotalsResponse,
  ServiceOverviewResponse,
  SpaceOverviewResponse,
} from "@/shared/api/generated/types.gen";
import { Section } from "@/shared/components/layout/layout";
import { MetricCard, MetricGrid } from "@/shared/components/metrics/metrics";
import { Badge } from "@/shared/ui/badge";
import { Card, CardContent } from "@/shared/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/table";

import { formatCount, formatPercent } from "./format";

export function OverviewMetrics({
  totals,
}: {
  totals: OverviewTotalsResponse;
}) {
  return (
    <MetricGrid>
      <MetricCard label="Аккаунты" value={totals.total} />
      <MetricCard
        label="Пригодны"
        value={totals.eligible}
        description={formatPercent(totals.eligible, totals.total)}
      />
      <MetricCard label="В аренде" value={totals.leased} />
      <MetricCard label="Свободны" value={totals.free_eligible} />
      <MetricCard label="Тяжёлые состояния" value={totals.heavy} />
      <MetricCard label="Открытые challenge" value={totals.open_challenges} />
      <MetricCard label="Требуют внимания" value={totals.action_required} />
    </MetricGrid>
  );
}

export function RegionDistribution({
  regions,
}: {
  regions: Record<string, Record<string, number>>;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {Object.entries(regions).map(([region, states]) => (
        <Card className="gap-2 py-3 shadow-none" key={region}>
          <CardContent className="grid gap-2 px-3">
            <strong className="text-sm">{region}</strong>
            <div className="flex flex-wrap gap-2">
              {Object.entries(states).map(([state, count]) => (
                <Badge key={state} variant="outline">
                  {state} · {formatCount(count)}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function SpaceLink({ space }: { space: SpaceOverviewResponse }) {
  return (
    <Link
      className="font-medium underline-offset-4 hover:underline"
      to="/spaces/$spaceSlug"
      params={{ spaceSlug: space.slug }}
      search={{}}
    >
      {space.title}
    </Link>
  );
}

export function CountWithPercent({
  value,
  total,
}: {
  value: number;
  total: number;
}) {
  return (
    <span className="block text-right font-mono tabular-nums">
      {formatCount(value)} · {formatPercent(value, total)}
    </span>
  );
}

export function SpaceSummaryList({
  spaces,
}: {
  spaces: SpaceOverviewResponse[];
}) {
  return (
    <div className="grid gap-3">
      {spaces.map((space) => (
        <Card className="py-4 shadow-none" key={space.id}>
          <CardContent className="grid gap-3 px-4 md:grid-cols-5 md:items-center">
            <div>
              <SpaceLink space={space} />
              <p className="text-muted-foreground text-xs">{space.slug}</p>
            </div>
            <span className="text-sm tabular-nums">
              Всего {formatCount(space.total)}
            </span>
            <span className="text-sm tabular-nums">
              Пригодны {formatPercent(space.eligible, space.total)}
            </span>
            <span className="text-sm tabular-nums">
              Аренда {formatCount(space.leased)}
            </span>
            <Link
              className="text-sm underline-offset-4 hover:underline"
              to="/spaces/$spaceSlug/accounts"
              params={{ spaceSlug: space.slug }}
              search={{ status: "attention" }}
            >
              Внимание {formatCount(space.action_required + space.heavy)}
            </Link>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function stateCount(
  space: SpaceOverviewResponse,
  region: string,
  state: string,
) {
  return space.services.reduce(
    (total, service) => total + (service.regions[region]?.[state] ?? 0),
    0,
  );
}

export function StateMatrix({
  spaces,
  region,
}: {
  spaces: SpaceOverviewResponse[];
  region: "standing" | "auth";
}) {
  const states = Array.from(
    new Set(
      spaces.flatMap((space) =>
        space.services.flatMap((service) =>
          Object.keys(service.regions[region] ?? {}),
        ),
      ),
    ),
  ).sort();

  return (
    <Section
      title={`Матрица space × ${region}`}
      description="Ячейка показывает текущее число аккаунтов."
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Пространство</TableHead>
            {states.map((state) => (
              <TableHead className="text-right" key={state}>
                {state}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {spaces.map((space) => (
            <TableRow key={space.id}>
              <TableCell>
                <SpaceLink space={space} />
              </TableCell>
              {states.map((state) => (
                <TableCell className="text-right font-mono" key={state}>
                  <Link
                    className="underline-offset-4 hover:underline"
                    to="/spaces/$spaceSlug/accounts"
                    params={{ spaceSlug: space.slug }}
                    search={{ status: `${region}:${state}` }}
                  >
                    {formatCount(stateCount(space, region, state))}
                  </Link>
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Section>
  );
}

export function ServiceOverview({
  service,
  spaceSlug,
}: {
  service: ServiceOverviewResponse;
  spaceSlug: string;
}) {
  return (
    <Section
      title={service.title}
      description={`${service.service} · ${formatCount(service.total)} аккаунтов · свободны ${formatCount(service.free_eligible)}`}
      actions={
        <Link
          className="text-sm underline-offset-4 hover:underline"
          to="/spaces/$spaceSlug"
          params={{ spaceSlug }}
          search={{ service: service.service }}
        >
          Только этот сервис
        </Link>
      }
    >
      <div className="grid gap-4">
        <RegionDistribution regions={service.regions} />
        {service.tiers.length === 0 ? null : (
          <div>
            <h3 className="mb-2 text-sm font-medium">Уровни верификации</h3>
            <div className="flex flex-wrap gap-2">
              {service.tiers.map((tier) => (
                <Badge key={tier.tier} variant="outline">
                  {tier.tier} · {formatCount(tier.count)}
                </Badge>
              ))}
            </div>
          </div>
        )}
        {service.factors.length === 0 ? null : (
          <div>
            <h3 className="mb-2 text-sm font-medium">Факторы</h3>
            <div className="grid gap-2 sm:grid-cols-2">
              {service.factors.map((factor) => (
                <Card className="gap-1 py-3 shadow-none" key={factor.factor}>
                  <CardContent className="px-3 text-sm">
                    <strong>{factor.factor}</strong>
                    <p className="text-muted-foreground mt-1">
                      {Object.entries(factor.states)
                        .map(
                          ([state, count]) => `${state} ${formatCount(count)}`,
                        )
                        .join(" · ")}
                    </p>
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        )}
        {service.requirements.length === 0 ? null : (
          <div>
            <h3 className="mb-2 text-sm font-medium">Открытые требования</h3>
            <div className="flex flex-wrap gap-2">
              {service.requirements.map((requirement) => (
                <Badge key={requirement.code} variant="outline">
                  {requirement.code} · {formatCount(requirement.count)}
                </Badge>
              ))}
            </div>
          </div>
        )}
      </div>
    </Section>
  );
}
