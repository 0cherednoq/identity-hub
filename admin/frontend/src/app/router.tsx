import {
  Outlet,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  lazyRouteComponent,
  redirect,
} from "@tanstack/react-router";

import type { AuthState } from "@/app/providers";
import { AdminErrorState } from "@/shared/components/states/admin-error-state";
import { AdminApiError } from "@/shared/api/transport";
import type { AccountSection } from "@/shared/api/accounts";
import type { AccountSearch } from "@/shared/lib/account-search";
import { parseHumanQueueSearch } from "@/shared/lib/human-queue-search";
import { parseAnalyticsSearch } from "@/shared/lib/analytics-search";
import { parseEventSearch } from "@/shared/lib/event-search";
import { parseLeaseSearch } from "@/shared/lib/lease-search";
import {
  parseProxyDetailSearch,
  parseProxySearch,
} from "@/shared/lib/proxy-search";

export type { AccountSearch } from "@/shared/lib/account-search";

export interface RouterContext {
  auth: AuthState;
}

export interface SpaceSearch {
  cursor?: string;
  q?: string;
  service?: string;
  status?: string;
}

export interface SpacesSearch {
  service?: string;
}

export interface DeliveryCenterSearch {
  tab?: "telegram" | "notifications" | "webhooks";
  notificationCursor?: string;
  webhookCursor?: string;
}

export function parseDeliveryCenterSearch(
  search: Record<string, unknown>,
): DeliveryCenterSearch {
  const tab =
    search.tab === "telegram" ||
    search.tab === "notifications" ||
    search.tab === "webhooks"
      ? search.tab
      : undefined;
  const notificationCursor =
    typeof search.notificationCursor === "string" &&
    search.notificationCursor !== ""
      ? search.notificationCursor
      : undefined;
  const webhookCursor =
    typeof search.webhookCursor === "string" && search.webhookCursor !== ""
      ? search.webhookCursor
      : undefined;
  return {
    ...(tab === undefined ? {} : { tab }),
    ...(notificationCursor === undefined ? {} : { notificationCursor }),
    ...(webhookCursor === undefined ? {} : { webhookCursor }),
  };
}

export function parseSpaceSearch(search: Record<string, unknown>): SpaceSearch {
  const stringValue = (key: keyof SpaceSearch) =>
    typeof search[key] === "string" && search[key] !== ""
      ? search[key]
      : undefined;
  const cursor = stringValue("cursor");
  const q = stringValue("q");
  const service = stringValue("service");
  const status = stringValue("status");
  return {
    ...(cursor === undefined ? {} : { cursor }),
    ...(q === undefined ? {} : { q }),
    ...(service === undefined ? {} : { service }),
    ...(status === undefined ? {} : { status }),
  };
}

export function parseSpacesSearch(
  search: Record<string, unknown>,
): SpacesSearch {
  return typeof search.service === "string" && search.service !== ""
    ? { service: search.service }
    : {};
}

export function parseAccountSearch(
  search: Record<string, unknown>,
): AccountSearch {
  const result: Record<string, string> = {};
  const keys: Array<keyof AccountSearch> = [
    "attention",
    "auth",
    "availability",
    "columns",
    "createdFrom",
    "createdTo",
    "cursor",
    "eligible",
    "factor",
    "factorState",
    "hasOverride",
    "lastLeasedFrom",
    "lastLeasedTo",
    "label",
    "leased",
    "lifecycle",
    "linked",
    "proxy",
    "quotaKind",
    "q",
    "requirement",
    "service",
    "standing",
    "standingChangedFrom",
    "standingChangedTo",
    "sort",
    "direction",
    "status",
    "syncedFrom",
    "syncedTo",
    "tag",
    "tier",
  ];
  for (const key of keys) {
    const value = search[key];
    if (typeof value === "string" && value !== "") result[key] = value;
  }
  const {
    attention,
    direction,
    eligible,
    hasOverride,
    leased,
    linked,
    ...strings
  } = result;
  const booleanValue = (value: unknown) => {
    if (value === true || value === "true") return true;
    if (value === false || value === "false") return false;
    return undefined;
  };
  const parsedAttention = booleanValue(search.attention ?? attention);
  const parsedEligible = booleanValue(search.eligible ?? eligible);
  const parsedHasOverride = booleanValue(search.hasOverride ?? hasOverride);
  const parsedLeased = booleanValue(search.leased ?? leased);
  const parsedLinked = booleanValue(search.linked ?? linked);
  const parsedDirection =
    direction === "asc" || direction === "desc" ? direction : undefined;
  return {
    ...strings,
    ...(parsedAttention === undefined ? {} : { attention: parsedAttention }),
    ...(parsedEligible === undefined ? {} : { eligible: parsedEligible }),
    ...(parsedHasOverride === undefined
      ? {}
      : { hasOverride: parsedHasOverride }),
    ...(parsedLeased === undefined ? {} : { leased: parsedLeased }),
    ...(parsedLinked === undefined ? {} : { linked: parsedLinked }),
    ...(parsedDirection === undefined ? {} : { direction: parsedDirection }),
  };
}

export interface AccountDetailSearch {
  tab?: AccountSection;
}

export type { LeaseSearch } from "@/shared/lib/lease-search";

export function parseAccountDetailSearch(
  search: Record<string, unknown>,
): AccountDetailSearch {
  const allowed: AccountSection[] = [
    "state",
    "profile",
    "access",
    "verification",
    "leases",
    "network",
    "events",
    "logs",
  ];
  return typeof search.tab === "string" &&
    allowed.includes(search.tab as AccountSection)
    ? { tab: search.tab as AccountSection }
    : {};
}

function safeRedirect(value: unknown): string {
  return typeof value === "string" &&
    value.startsWith("/") &&
    !value.startsWith("//")
    ? value
    : "/";
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: Outlet,
  errorComponent: ({ error, reset }) => (
    <AdminErrorState error={error} onRetry={reset} />
  ),
  notFoundComponent: () => (
    <AdminErrorState
      error={
        new AdminApiError(404, {
          error: {
            code: "not_found",
            message: "Страница не найдена",
            request_id: "router-not-found",
          },
        })
      }
    />
  ),
});

const designRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/_design",
  component: lazyRouteComponent(() => import("@/pages/design"), "DesignPage"),
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: (search) => ({ redirect: safeRedirect(search.redirect) }),
  component: lazyRouteComponent(() => import("@/pages/login"), "LoginPage"),
});

const authenticatedRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "_authenticated",
  beforeLoad: ({ context, location }) => {
    if (context.auth.principal === null) {
      throw redirect({ to: "/login", search: { redirect: location.href } });
    }
  },
  component: lazyRouteComponent(
    () => import("@/app/shell/app-shell"),
    "AppShell",
  ),
});

const overviewRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/",
  component: lazyRouteComponent(() => import("@/pages/home"), "HomePage"),
});

const spacesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces",
  validateSearch: parseSpacesSearch,
  component: lazyRouteComponent(() => import("@/pages/spaces"), "SpacesPage"),
});

const servicesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/services",
  component: lazyRouteComponent(
    () => import("@/pages/services"),
    "ServicesPage",
  ),
});

const backgroundRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/background",
  component: lazyRouteComponent(
    () => import("@/pages/background"),
    "BackgroundPage",
  ),
});

const serviceDetailRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/services/$serviceName",
  component: lazyRouteComponent(
    () => import("@/pages/service-detail"),
    "ServiceDetailPage",
  ),
});

const spaceRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug",
  validateSearch: parseSpaceSearch,
  component: lazyRouteComponent(
    () => import("@/pages/space-overview"),
    "SpaceOverviewPage",
  ),
});

const accountsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/accounts",
  validateSearch: parseAccountSearch,
  component: lazyRouteComponent(
    () => import("@/pages/accounts"),
    "AccountsPage",
  ),
});

const importsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/imports",
  component: lazyRouteComponent(() => import("@/pages/imports"), "ImportsPage"),
});

const accountDetailRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/accounts/$identityId",
  validateSearch: parseAccountDetailSearch,
  component: lazyRouteComponent(
    () => import("@/pages/account-detail"),
    "AccountDetailPage",
  ),
});

const leasesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/leases",
  validateSearch: parseLeaseSearch,
  component: lazyRouteComponent(() => import("@/pages/leases"), "LeasesPage"),
});

const analyticsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/analytics",
  validateSearch: parseAnalyticsSearch,
  component: lazyRouteComponent(
    () => import("@/pages/analytics"),
    "AnalyticsPage",
  ),
});

const humanQueueRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/human-queue",
  validateSearch: parseHumanQueueSearch,
  component: lazyRouteComponent(
    () => import("@/pages/human-queue"),
    "HumanQueuePage",
  ),
});

const proxiesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/proxies",
  validateSearch: parseProxySearch,
  component: lazyRouteComponent(() => import("@/pages/proxies"), "ProxiesPage"),
});

const eventsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/events",
  validateSearch: parseEventSearch,
  component: lazyRouteComponent(() => import("@/pages/events"), "EventsPage"),
});

const deliveryCenterRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/delivery-center",
  validateSearch: parseDeliveryCenterSearch,
  component: lazyRouteComponent(
    () => import("@/pages/delivery-center"),
    "DeliveryCenterPage",
  ),
});

const proxyDetailRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: "/spaces/$spaceSlug/proxies/$proxyId",
  validateSearch: parseProxyDetailSearch,
  component: lazyRouteComponent(
    () => import("@/pages/proxy-detail"),
    "ProxyDetailPage",
  ),
});

const routeTree = rootRoute.addChildren([
  designRoute,
  loginRoute,
  authenticatedRoute.addChildren([
    overviewRoute,
    backgroundRoute,
    serviceDetailRoute,
    servicesRoute,
    spacesRoute,
    accountDetailRoute,
    accountsRoute,
    analyticsRoute,
    deliveryCenterRoute,
    eventsRoute,
    humanQueueRoute,
    importsRoute,
    leasesRoute,
    proxiesRoute,
    proxyDetailRoute,
    spaceRoute,
  ]),
]);

export const router = createRouter({
  routeTree,
  context: { auth: { error: null, isPending: true, principal: null } },
  defaultPreload: "intent",
  scrollRestoration: true,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
