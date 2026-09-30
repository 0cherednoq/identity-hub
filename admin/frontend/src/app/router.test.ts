import { describe, expect, it } from "vitest";

import {
  parseAccountSearch,
  parseSpaceSearch,
  parseSpacesSearch,
} from "./router";

describe("typed space search", () => {
  it("keeps supported string filters and drops unsafe shapes", () => {
    expect(
      parseSpaceSearch({
        q: "alice",
        service: "telegram",
        status: "ready",
        cursor: 42,
        extra: "no",
      }),
    ).toEqual({ q: "alice", service: "telegram", status: "ready" });
    expect(parseSpacesSearch({ service: ["unsafe"] })).toEqual({});
    expect(parseSpacesSearch({ service: "telegram" })).toEqual({
      service: "telegram",
    });
  });
});

describe("typed account search", () => {
  it("keeps supported URL presets and rejects invalid boolean values", () => {
    expect(
      parseAccountSearch({
        attention: "true",
        columns: "service,statuses",
        eligible: "maybe",
        linked: "false",
        q: "olena",
        extra: "no",
      }),
    ).toEqual({
      attention: true,
      columns: "service,statuses",
      linked: false,
      q: "olena",
    });
  });
});
