export interface LeaseSearch {
  cursor?: string;
  groupBy?: "pool" | "service" | "holder";
  holder?: string;
  pool?: string;
  service?: string;
}

export type LeaseSearchPatch = {
  [Key in keyof LeaseSearch]?: LeaseSearch[Key] | undefined;
};

export function parseLeaseSearch(search: Record<string, unknown>): LeaseSearch {
  const value = (key: keyof LeaseSearch) =>
    typeof search[key] === "string" && search[key] !== ""
      ? search[key]
      : undefined;
  const rawGroup = value("groupBy");
  const cursor = value("cursor");
  const holder = value("holder");
  const pool = value("pool");
  const service = value("service");
  const groupBy =
    rawGroup === "pool" || rawGroup === "service" || rawGroup === "holder"
      ? rawGroup
      : undefined;
  return {
    ...(cursor === undefined ? {} : { cursor }),
    ...(groupBy === undefined ? {} : { groupBy }),
    ...(holder === undefined ? {} : { holder }),
    ...(pool === undefined ? {} : { pool }),
    ...(service === undefined ? {} : { service }),
  };
}

export function mergeLeaseSearch(
  previous: LeaseSearch,
  patch: LeaseSearchPatch,
): LeaseSearch {
  const selected = <Key extends keyof LeaseSearch>(key: Key) =>
    key in patch ? patch[key] : previous[key];
  const groupBy = selected("groupBy");
  const holder = selected("holder");
  const pool = selected("pool");
  const service = selected("service");
  const cursor = patch.cursor;
  return {
    ...(cursor === undefined || cursor === "" ? {} : { cursor }),
    ...(groupBy === undefined ? {} : { groupBy }),
    ...(holder === undefined || holder === "" ? {} : { holder }),
    ...(pool === undefined || pool === "" ? {} : { pool }),
    ...(service === undefined || service === "" ? {} : { service }),
  };
}
