export interface ProxySearch {
  capacity?: "available" | "full" | "unbound";
  cursor?: string;
  pool?: string;
  status?: "alive" | "quarantined" | "dead" | "unknown";
}

export type ProxySearchPatch = {
  [Key in keyof ProxySearch]?: ProxySearch[Key] | undefined;
};

export function parseProxySearch(search: Record<string, unknown>): ProxySearch {
  const stringValue = (key: keyof ProxySearch) =>
    typeof search[key] === "string" && search[key] !== ""
      ? search[key]
      : undefined;
  const rawCapacity = stringValue("capacity");
  const rawStatus = stringValue("status");
  const capacity = ["available", "full", "unbound"].includes(rawCapacity ?? "")
    ? (rawCapacity as ProxySearch["capacity"])
    : undefined;
  const status = ["alive", "quarantined", "dead", "unknown"].includes(
    rawStatus ?? "",
  )
    ? (rawStatus as ProxySearch["status"])
    : undefined;
  const cursor = stringValue("cursor");
  const pool = stringValue("pool");
  return {
    ...(capacity === undefined ? {} : { capacity }),
    ...(cursor === undefined ? {} : { cursor }),
    ...(pool === undefined ? {} : { pool }),
    ...(status === undefined ? {} : { status }),
  };
}

export function mergeProxySearch(
  previous: ProxySearch,
  patch: ProxySearchPatch,
): ProxySearch {
  const selected = <Key extends keyof ProxySearch>(key: Key) =>
    key in patch ? patch[key] : previous[key];
  const capacity = selected("capacity");
  const pool = selected("pool");
  const status = selected("status");
  const cursor = patch.cursor;
  return {
    ...(capacity === undefined ? {} : { capacity }),
    ...(cursor === undefined || cursor === "" ? {} : { cursor }),
    ...(pool === undefined || pool === "" ? {} : { pool }),
    ...(status === undefined ? {} : { status }),
  };
}

export interface ProxyDetailSearch {
  historyCursor?: string;
}

export function parseProxyDetailSearch(
  search: Record<string, unknown>,
): ProxyDetailSearch {
  return typeof search.historyCursor === "string" && search.historyCursor !== ""
    ? { historyCursor: search.historyCursor }
    : {};
}
