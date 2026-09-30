import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { downloadExport, executeBulk, previewBulk } from "@/shared/api/bulk";

import { BulkControls } from "./bulk-controls";

vi.mock("@/shared/api/bulk", () => ({
  downloadExport: vi.fn(),
  executeBulk: vi.fn(),
  previewBulk: vi.fn(),
}));

describe("bulk controls", () => {
  beforeEach(() => {
    vi.mocked(previewBulk)
      .mockReset()
      .mockResolvedValue({
        snapshot_id: "00000000-0000-0000-0000-000000003699",
        operation: "tag_add",
        selected_count: 2,
        truncated: false,
        expires_at: "2026-09-27T12:15:00Z",
        sample: [
          {
            identity_id: "00000000-0000-0000-0000-000000003601",
            service: "demo_account",
            external_ref: "alpha",
            revision: 4,
          },
        ],
      });
    vi.mocked(executeBulk).mockReset().mockResolvedValue({
      snapshot_id: "00000000-0000-0000-0000-000000003699",
      operation: "tag_add",
      total: 2,
      succeeded: 2,
      failed: 0,
      skipped_already_succeeded: 0,
      attempt: 1,
      complete: true,
      failures: [],
    });
    vi.mocked(downloadExport).mockReset().mockResolvedValue();
  });

  afterEach(() => {
    cleanup();
  });

  it("previews the server-side filter before executing the frozen snapshot", async () => {
    const onComplete = vi.fn();
    render(
      <BulkControls
        filter={{ tag: "selected", sort: "created_at", direction: "desc" }}
        spaceSlug="demo-large"
        onComplete={onComplete}
      />,
    );
    fireEvent.change(screen.getByLabelText("Причина"), {
      target: { value: "scheduled batch" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview batch" }));
    await waitFor(() => {
      expect(screen.getByText(/Зафиксировано: 2/)).toBeInTheDocument();
    });
    expect(vi.mocked(previewBulk).mock.calls[0]?.[0].filter.tag).toBe(
      "selected",
    );

    fireEvent.click(
      screen.getByLabelText(
        "Я подтверждаю изменение зафиксированного набора аккаунтов",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Выполнить batch" }));
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("успешно 2 / 2");
    });
    expect(executeBulk).toHaveBeenCalledWith(
      expect.objectContaining({
        snapshotId: "00000000-0000-0000-0000-000000003699",
      }),
    );
    expect(onComplete).toHaveBeenCalledOnce();
  });
});
