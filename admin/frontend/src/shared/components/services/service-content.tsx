import { Link } from "@tanstack/react-router";

import type { ServiceDescriptorResponse } from "@/shared/api/generated/types.gen";
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

function Support({ value }: { value: boolean }) {
  return (
    <Badge variant={value ? "secondary" : "outline"}>
      {value ? "Поддерживается" : "Не поддерживается"}
    </Badge>
  );
}

export function ServiceCatalogContent({
  items,
}: {
  items: ServiceDescriptorResponse[];
}) {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {items.map((item) => (
        <Card key={item.name}>
          <CardHeader>
            <CardTitle>
              <Link
                className="underline-offset-4 hover:underline"
                to="/services/$serviceName"
                params={{ serviceName: item.name }}
              >
                {item.title}
              </Link>
            </CardTitle>
            <p className="text-muted-foreground font-mono text-xs">
              {item.name}
            </p>
          </CardHeader>
          <CardContent className="grid gap-2 text-sm">
            <p>{item.total} аккаунтов в доступных пространствах</p>
            <p>
              {item.operations.length} операций · {item.verifications.length}{" "}
              факторов
            </p>
            <div className="flex flex-wrap gap-2">
              <Badge variant="outline">{item.capabilities.lease_mode}</Badge>
              <Support value={item.proxy_policy.supported} />
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function ServiceDetailContent({
  item,
}: {
  item: ServiceDescriptorResponse;
}) {
  const capabilityRows: Array<[string, boolean]> = [
    ["Аутентификатор", item.capabilities.authenticator],
    ["Пробы", item.capabilities.probes],
    ["Классификатор ошибок", item.capabilities.error_classifier],
    ["Импорт", item.capabilities.importers],
    ["Зашифрованный профиль", item.capabilities.profile_encrypted],
  ];
  return (
    <div className="grid gap-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Доступные пространства</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2 text-sm">
            {item.spaces.length === 0 ? (
              <p className="text-muted-foreground">
                Нет доступных пространств.
              </p>
            ) : (
              item.spaces.map((space) => (
                <Link
                  key={space.space_slug}
                  className="flex justify-between underline-offset-4 hover:underline"
                  to="/spaces/$spaceSlug/accounts"
                  params={{ spaceSlug: space.space_slug }}
                  search={{ service: item.name }}
                >
                  <span>{space.space_slug}</span>
                  <span>{space.count}</span>
                </Link>
              ))
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Возможности</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2 text-sm">
            {capabilityRows.map(([label, supported]) => (
              <div
                key={label}
                className="flex items-center justify-between gap-2"
              >
                <span>{label}</span>
                <Support value={supported} />
              </div>
            ))}
            <p>
              Режим аренды:{" "}
              <Badge variant="outline">{item.capabilities.lease_mode}</Badge>
            </p>
            <p>Импортёров: {item.capabilities.importer_count}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Политика прокси</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2 text-sm">
            <Support value={item.proxy_policy.supported} />
            {item.proxy_policy.supported ? (
              <>
                <p>Пул: {item.proxy_policy.pool}</p>
                <p>Sticky: {item.proxy_policy.sticky ? "да" : "нет"}</p>
                <p>
                  Максимум аккаунтов:{" "}
                  {item.proxy_policy.max_identities ?? "из прокси"}
                </p>
                <p>При отказе: {item.proxy_policy.on_proxy_down}</p>
              </>
            ) : (
              <p className="text-muted-foreground">
                Сервис не использует прокси.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Операции · {item.operations.length}</CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Операция</TableHead>
                <TableHead>Авторизация</TableHead>
                <TableHead>Standing</TableHead>
                <TableHead>Факторы / tiers</TableHead>
                <TableHead>Квоты</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {item.operations.map((operation) => (
                <TableRow key={operation.kind}>
                  <TableCell className="font-mono">{operation.kind}</TableCell>
                  <TableCell>
                    {operation.needs_auth ? "нужна" : "не нужна"}
                  </TableCell>
                  <TableCell>{operation.standing.join(", ") || "—"}</TableCell>
                  <TableCell>
                    {[...operation.verified, ...operation.tiers].join(", ") ||
                      "не требуются"}
                  </TableCell>
                  <TableCell>
                    {operation.quotas.length === 0
                      ? "Без квот"
                      : operation.quotas
                          .map(
                            (quota) =>
                              `${String(quota.limit)} / ${String(quota.window_seconds)}с`,
                          )
                          .join("; ")}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Верификации и tiers</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 text-sm">
            {item.verifications.length === 0 ? (
              <p className="text-muted-foreground">Не поддерживаются.</p>
            ) : (
              item.verifications.map((factor) => (
                <div key={factor.name} className="rounded-md border p-3">
                  <p className="font-medium">
                    {factor.name} · {factor.mode}
                  </p>
                  <p>Зависит от: {factor.requires.join(", ") || "ничего"}</p>
                  <p>
                    Попыток: {factor.max_attempts ?? "без лимита"} · evidence:{" "}
                    {factor.evidence_required ? "да" : "нет"}
                  </p>
                  <p>Коды отказа: {factor.reject_codes.join(", ") || "нет"}</p>
                </div>
              ))
            )}
            <div className="flex flex-wrap gap-2">
              {item.tiers.length === 0 ? (
                <Badge variant="outline">Tiers не поддерживаются</Badge>
              ) : (
                item.tiers.map((tier) => (
                  <Badge key={tier.name} variant="outline">
                    {tier.name}: {tier.factors.join(" + ")}
                  </Badge>
                ))
              )}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Коды standing</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2 text-sm">
            {item.standing_codes.length === 0 ? (
              <p className="text-muted-foreground">
                Собственные коды не объявлены.
              </p>
            ) : (
              item.standing_codes.map((code) => (
                <div key={code.code} className="flex justify-between gap-2">
                  <code>{code.code}</code>
                  <Badge variant="outline">{code.standing}</Badge>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Схемы данных</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          {item.schemas.map((schema) => (
            <div key={schema.category} className="rounded-md border p-3">
              <h3 className="font-medium">{schema.category}</h3>
              {schema.fields.length === 0 ? (
                <p className="text-muted-foreground text-sm">Полей нет.</p>
              ) : (
                <ul className="mt-2 grid gap-1 text-sm">
                  {schema.fields.map((field) => (
                    <li
                      key={field.name}
                      className="flex flex-wrap items-center gap-2"
                    >
                      <code>{field.name}</code>
                      <span>{field.kind}</span>
                      {field.required ? (
                        <Badge variant="outline">required</Badge>
                      ) : null}
                      {field.sensitive ? <Badge>чувствительное</Badge> : null}
                      {field.filterable ? (
                        <Badge variant="secondary">filter</Badge>
                      ) : null}
                      {field.searchable ? (
                        <Badge variant="secondary">search</Badge>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
