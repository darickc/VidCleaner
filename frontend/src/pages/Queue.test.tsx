import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EMPTY_QUEUE, HEALTH, item, job, mockApi } from "../test/api";
import { renderApp } from "../test/render";
import { QueuePage } from "./Queue";

afterEach(() => vi.unstubAllGlobals());

const RUNNING = job({
  id: "run-1",
  state: "transcribing",
  stage: "transcribe",
  progress_pct: 42.5,
  started_at: new Date().toISOString(),
  trigger: "webhook",
});

const QUEUED = job({
  id: "wait-1",
  state: "queued",
  priority: 200,
  item: item({ id: 9, label: "Heat (1995)", kind: "movie" }),
});

const FAILED = job({
  id: "gone-1",
  state: "failed",
  error: "verify: clean track was audible in a mute window",
  finished_at: new Date().toISOString(),
});

function stub(queue: unknown, extra: Record<string, unknown> = {}) {
  return mockApi({ routes: { "/health": HEALTH, "/jobs": queue, ...extra } });
}

describe("queue page", () => {
  it("shows the running job with its stage and progress", async () => {
    stub({ ...EMPTY_QUEUE, running: [RUNNING] });
    renderApp(<QueuePage />);

    await waitFor(() => expect(screen.getByText("transcribe")).toBeInTheDocument());
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "43");
    expect(screen.getByRole("link", { name: RUNNING.item.label })).toHaveAttribute(
      "href",
      "/items/7",
    );
  });

  it("says so when nothing is running", async () => {
    stub(EMPTY_QUEUE);
    renderApp(<QueuePage />);
    await waitFor(() => expect(screen.getByText("Nothing is running.")).toBeInTheDocument());
    expect(screen.getByText("The queue is empty.")).toBeInTheDocument();
  });

  it("cancels a queued job", async () => {
    const api = stub(
      { ...EMPTY_QUEUE, queued: [QUEUED], queued_total: 1 },
      { "/jobs/wait-1/cancel": { cancelled: true } },
    );
    renderApp(<QueuePage />);

    await screen.findByRole("button", { name: "Cancel Heat (1995)" });
    await userEvent.click(screen.getByRole("button", { name: "Cancel Heat (1995)" }));

    await waitFor(() =>
      expect(api.calls.some((c) => c.path === "/jobs/wait-1/cancel" && c.method === "POST")).toBe(
        true,
      ),
    );
  });

  it("moves a queued job to the front", async () => {
    const api = stub(
      { ...EMPTY_QUEUE, queued: [QUEUED], queued_total: 1 },
      { "/jobs/wait-1": { updated: true } },
    );
    renderApp(<QueuePage />);

    await userEvent.click(await screen.findByRole("button", { name: "Run Heat (1995) next" }));

    await waitFor(() => {
      const call = api.calls.find((c) => c.method === "PATCH");
      expect(call?.path).toBe("/jobs/wait-1");
      // Lower runs sooner, so "run next" must go *below* every trigger default.
      expect((call?.body as { priority: number }).priority).toBeLessThan(50);
    });
  });

  it("offers retry only for jobs that failed", async () => {
    stub({ ...EMPTY_QUEUE, recent: [FAILED, job({ id: "ok-1", state: "done" })] });
    renderApp(<QueuePage />);

    await waitFor(() => expect(screen.getByText("failed")).toBeInTheDocument());
    expect(screen.getByText(/clean track was audible/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Retry/ })).toHaveLength(1);
  });

  it("says how many queued jobs are not shown", async () => {
    stub({ ...EMPTY_QUEUE, queued: [QUEUED], queued_total: 137 });
    renderApp(<QueuePage />);
    await waitFor(() => expect(screen.getByText("showing 1 of 137")).toBeInTheDocument());
  });

  it("warns when a volume is nearly full", async () => {
    const tight = {
      ...HEALTH,
      disk: {
        ...HEALTH.disk,
        work: { path: "/work", exists: true, free_bytes: 2 ** 30, total_bytes: 2 ** 40 * 1 },
      },
    };
    mockApi({ routes: { "/health": tight, "/jobs": EMPTY_QUEUE } });
    renderApp(<QueuePage />);
    // 1 GiB of a 1 TiB volume is under the 5% mark.
    await waitFor(() => expect(screen.getAllByText("1.0 GiB free").length).toBeGreaterThan(0));
  });
});
