import { describe, expect, it } from "vitest";

import {
  clearSessionCache,
  createAdminQueryClient,
  invalidateSpaceQueries,
} from "./query-client";

describe("admin query cache boundaries", () => {
  it("invalidates only the previous space scope", async () => {
    const client = createAdminQueryClient();
    client.setQueryData(["space", "alpha", "accounts"], [1]);
    client.setQueryData(["space", "beta", "accounts"], [2]);

    await invalidateSpaceQueries(client, "alpha");

    expect(
      client.getQueryState(["space", "alpha", "accounts"])?.isInvalidated,
    ).toBe(true);
    expect(
      client.getQueryState(["space", "beta", "accounts"])?.isInvalidated,
    ).toBe(false);
  });

  it("clears query and mutation caches on logout", () => {
    const client = createAdminQueryClient();
    client.setQueryData(["auth", "principal"], { actor: "test" });
    clearSessionCache(client);
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    expect(client.getMutationCache().getAll()).toHaveLength(0);
  });
});
