export interface EventSearch {
  actor?: string;
  cursor?: string;
  group?: string;
  identityId?: string;
  includeJournal?: boolean;
  leaseId?: string;
  occurredFrom?: string;
  occurredTo?: string;
  proxyId?: string;
  selected?: string;
  service?: string;
  severity?: "info" | "warning" | "critical";
  type?: string;
}

export type EventSearchPatch = {
  [Key in keyof EventSearch]?: EventSearch[Key] | undefined;
};

export function parseEventSearch(search: Record<string, unknown>): EventSearch {
  const value = (key: string) =>
    typeof search[key] === "string" && search[key] !== ""
      ? search[key]
      : undefined;
  const severityValue = value("severity");
  const severity = ["info", "warning", "critical"].includes(severityValue ?? "")
    ? (severityValue as NonNullable<EventSearch["severity"]>)
    : undefined;
  const includeJournal =
    search.includeJournal === true || search.includeJournal === "true";
  const actor = value("actor");
  const cursor = value("cursor");
  const group = value("group");
  const identityId = value("identityId");
  const leaseId = value("leaseId");
  const occurredFrom = value("occurredFrom");
  const occurredTo = value("occurredTo");
  const proxyId = value("proxyId");
  const selected = value("selected");
  const service = value("service");
  const type = value("type");
  return {
    ...(actor === undefined ? {} : { actor }),
    ...(cursor === undefined ? {} : { cursor }),
    ...(group === undefined ? {} : { group }),
    ...(identityId === undefined ? {} : { identityId }),
    ...(includeJournal ? { includeJournal: true } : {}),
    ...(leaseId === undefined ? {} : { leaseId }),
    ...(occurredFrom === undefined ? {} : { occurredFrom }),
    ...(occurredTo === undefined ? {} : { occurredTo }),
    ...(proxyId === undefined ? {} : { proxyId }),
    ...(selected === undefined ? {} : { selected }),
    ...(service === undefined ? {} : { service }),
    ...(severity === undefined ? {} : { severity }),
    ...(type === undefined ? {} : { type }),
  };
}

export function mergeEventSearch(
  previous: EventSearch,
  patch: EventSearchPatch,
): EventSearch {
  const filterChanged = Object.keys(patch).some(
    (key) => key !== "cursor" && key !== "selected",
  );
  const result: EventSearch = {};
  const keys: Array<keyof EventSearch> = [
    "actor",
    "cursor",
    "group",
    "identityId",
    "leaseId",
    "occurredFrom",
    "occurredTo",
    "proxyId",
    "selected",
    "service",
    "type",
    "includeJournal",
    "severity",
  ];
  for (const key of keys) {
    if (key === "cursor" && filterChanged) continue;
    const value = key in patch ? patch[key] : previous[key];
    if (value !== undefined && value !== "" && value !== false) {
      Object.assign(result, { [key]: value });
    }
  }
  return result;
}
