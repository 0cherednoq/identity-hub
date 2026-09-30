import { useEffect } from "react";
import { Link } from "@tanstack/react-router";

import type { EventItemResponse } from "@/shared/api/generated/types.gen";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";

export function EventDrawer({
  item,
  spaceSlug,
  onClose,
}: {
  item: EventItemResponse;
  spaceSlug: string;
  onClose: () => void;
}) {
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button
        type="button"
        className="bg-foreground absolute inset-0 opacity-40"
        aria-label="Закрыть подробности события"
        onClick={onClose}
      />
      <aside
        className="bg-background relative z-10 h-full w-full max-w-2xl overflow-y-auto border-l p-6 shadow-sm"
        role="dialog"
        aria-modal="true"
        aria-labelledby="event-drawer-title"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-muted-foreground text-sm">{item.group}</p>
            <h2
              id="event-drawer-title"
              className="font-mono text-lg font-semibold"
            >
              {item.type}
            </h2>
          </div>
          <Button type="button" variant="outline" onClick={onClose}>
            Закрыть
          </Button>
        </div>
        <dl className="mt-6 grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-muted-foreground">Важность</dt>
            <dd>
              <Badge>{item.severity}</Badge>
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Время</dt>
            <dd>{new Date(item.occurred_at).toLocaleString("ru-RU")}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Actor</dt>
            <dd>{item.actor}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Event ID</dt>
            <dd className="font-mono text-xs break-all">{item.event_id}</dd>
          </div>
        </dl>
        <div className="mt-6 flex flex-wrap gap-3 text-sm">
          {item.identity_id === null ? null : (
            <Link
              className="underline"
              to="/spaces/$spaceSlug/accounts/$identityId"
              params={{ spaceSlug, identityId: item.identity_id }}
              search={{ tab: "events" }}
            >
              Открыть аккаунт
            </Link>
          )}
          {item.proxy_id === null ? null : (
            <Link
              className="underline"
              to="/spaces/$spaceSlug/proxies/$proxyId"
              params={{ spaceSlug, proxyId: item.proxy_id }}
              search={{}}
            >
              Открыть прокси
            </Link>
          )}
          {item.lease_id === null ? null : (
            <Link
              className="underline"
              to="/spaces/$spaceSlug/leases"
              params={{ spaceSlug }}
              search={{}}
            >
              Открыть аренды
            </Link>
          )}
        </div>
        <h3 className="mt-8 font-semibold">Redacted JSON</h3>
        <pre className="bg-muted mt-2 overflow-x-auto rounded-md p-4 text-xs">
          {JSON.stringify(item.data, null, 2)}
        </pre>
      </aside>
    </div>
  );
}
