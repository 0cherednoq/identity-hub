export const humanQueueKinds = [
  "challenge",
  "requirement",
  "manual_review",
  "drift",
  "quarantine",
] as const;

export const humanQueueUrgencies = ["critical", "high", "medium"] as const;

export type HumanQueueKind = (typeof humanQueueKinds)[number];
export type HumanQueueUrgency = (typeof humanQueueUrgencies)[number];

export interface HumanQueueSearch {
  cursor?: string;
  kind?: HumanQueueKind;
  service?: string;
  urgency?: HumanQueueUrgency;
}

export type HumanQueueSearchPatch = {
  [Key in keyof HumanQueueSearch]?: HumanQueueSearch[Key] | undefined;
};

export function parseHumanQueueSearch(
  search: Record<string, unknown>,
): HumanQueueSearch {
  const stringValue = (key: keyof HumanQueueSearch) =>
    typeof search[key] === "string" && search[key] !== ""
      ? search[key]
      : undefined;
  const rawKind = stringValue("kind");
  const rawUrgency = stringValue("urgency");
  const cursor = stringValue("cursor");
  const service = stringValue("service");
  const kind = humanQueueKinds.find((value) => value === rawKind);
  const urgency = humanQueueUrgencies.find((value) => value === rawUrgency);
  return {
    ...(cursor === undefined ? {} : { cursor }),
    ...(kind === undefined ? {} : { kind }),
    ...(service === undefined ? {} : { service }),
    ...(urgency === undefined ? {} : { urgency }),
  };
}

export function mergeHumanQueueSearch(
  previous: HumanQueueSearch,
  patch: HumanQueueSearchPatch,
): HumanQueueSearch {
  const selected = <Key extends keyof HumanQueueSearch>(key: Key) =>
    key in patch ? patch[key] : previous[key];
  const kind = selected("kind");
  const service = selected("service");
  const urgency = selected("urgency");
  const cursor = patch.cursor;
  return {
    ...(cursor === undefined || cursor === "" ? {} : { cursor }),
    ...(kind === undefined ? {} : { kind }),
    ...(service === undefined || service === "" ? {} : { service }),
    ...(urgency === undefined ? {} : { urgency }),
  };
}
