import { describe, expect, it } from "vitest";

import {
  mergeProxySearch,
  parseProxyDetailSearch,
  parseProxySearch,
} from "./proxy-search";

describe("proxy URL state", () => {
  it("keeps supported filters and drops invalid values", () => {
    expect(
      parseProxySearch({
        capacity: "available",
        cursor: "next-page",
        pool: "residential-ua",
        status: "alive",
        extra: "ignored",
      }),
    ).toEqual({
      capacity: "available",
      cursor: "next-page",
      pool: "residential-ua",
      status: "alive",
    });
    expect(parseProxySearch({ capacity: "empty", status: "offline" })).toEqual(
      {},
    );
  });

  it("resets the cursor whenever filters change", () => {
    expect(
      mergeProxySearch(
        { cursor: "old", pool: "residential-ua", status: "alive" },
        { capacity: "full" },
      ),
    ).toEqual({
      capacity: "full",
      pool: "residential-ua",
      status: "alive",
    });
  });

  it("accepts only a non-empty history cursor", () => {
    expect(parseProxyDetailSearch({ historyCursor: "history-next" })).toEqual({
      historyCursor: "history-next",
    });
    expect(parseProxyDetailSearch({ historyCursor: 42 })).toEqual({});
  });
});
