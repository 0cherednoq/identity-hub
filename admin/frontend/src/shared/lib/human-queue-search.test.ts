import { describe, expect, it } from "vitest";

import {
  mergeHumanQueueSearch,
  parseHumanQueueSearch,
} from "./human-queue-search";

describe("human queue URL state", () => {
  it("keeps supported filters and drops unknown values", () => {
    expect(
      parseHumanQueueSearch({
        cursor: "next-page",
        kind: "requirement",
        service: "fake_exchange",
        urgency: "high",
        extra: "no",
      }),
    ).toEqual({
      cursor: "next-page",
      kind: "requirement",
      service: "fake_exchange",
      urgency: "high",
    });
    expect(
      parseHumanQueueSearch({ kind: "secret", urgency: "urgent" }),
    ).toEqual({});
  });

  it("resets the cursor whenever filters are patched", () => {
    expect(
      mergeHumanQueueSearch(
        { cursor: "old", kind: "challenge", service: "tg" },
        { service: "exchange" },
      ),
    ).toEqual({ kind: "challenge", service: "exchange" });
  });
});
