import { describe, expect, it } from "vitest";

import { loadPreferences, saveDensity, saveTheme } from "./preferences";

describe("safe preferences", () => {
  it("persists only explicit theme and density values", () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    };
    saveTheme(storage, "dark");
    saveDensity(storage, "compact");
    expect(loadPreferences(storage)).toEqual({
      theme: "dark",
      density: "compact",
    });
    expect([...values.keys()].sort()).toEqual([
      "ih-admin:density",
      "ih-admin:theme",
    ]);
  });
});
