import { describe, expect, it } from "vitest";

import {
  estimateImportRecords,
  importFormat,
  parseBatchLabels,
  prepareImportFile,
} from "./import-file";

describe("import file preparation", () => {
  it("detects supported formats and counts non-empty records", () => {
    expect(importFormat("ACCOUNTS.JSONL")).toBe("jsonl");
    expect(importFormat("accounts.txt")).toBeNull();
    expect(estimateImportRecords("external_ref\na\n\nb\n", "csv")).toBe(2);
    expect(estimateImportRecords('{"external_ref":"a"}\n\n', "jsonl")).toBe(1);
  });

  it("prepares a small file without retaining an object URL", async () => {
    const result = await prepareImportFile(
      new File(['{"external_ref":"a"}\n'], "batch.jsonl"),
    );
    expect(result).toMatchObject({
      format: "jsonl",
      name: "batch.jsonl",
      records: 1,
    });
  });

  it("parses batch labels and rejects ambiguous input", () => {
    expect(parseBatchLabels("team=ops, source=migration=v2")).toEqual({
      team: "ops",
      source: "migration=v2",
    });
    expect(() => parseBatchLabels("missing-value")).toThrow(/key=value/u);
  });
});
