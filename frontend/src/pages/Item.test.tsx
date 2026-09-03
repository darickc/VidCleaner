import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { detection, item, job, mockApi, title } from "../test/api";
import { renderApp } from "../test/render";
import { ItemPage } from "./Item";

afterEach(() => vi.unstubAllGlobals());

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useParams: () => ({ itemId: "7" }) };
});

const DETAIL = {
  item: item(),
  title: title(),
  job: job({ state: "done", model_used: "large-v3-turbo", subtitle_source: "embedded eng" }),
  jobs: [job({ state: "done" })],
  counts: [{ word_canonical: "shit", category: "strong", total: 2, muted: 2, suspicious: 0 }],
  detections: [
    detection(),
    detection({
      id: 2,
      word_raw: "bass",
      word_canonical: "bass",
      start_s: 240.1,
      source: "stt",
      suspicious: true,
      snippet: null,
    }),
  ],
  whitelist: [{ id: 5, scope: "global", scope_id: null, canonical_word: "class", context_text: null }],
  backups: [],
  restorable: false,
};

function stub(overrides: Record<string, unknown> = {}, extra: Record<string, unknown> = {}) {
  return mockApi({ routes: { "/items/7": { ...DETAIL, ...overrides }, ...extra } });
}

const render = () => renderApp(<ItemPage />, { route: "/items/7" });

describe("item page", () => {
  it("summarises how the file was cleaned", async () => {
    stub();
    render();

    await waitFor(() => expect(screen.getByText("large-v3-turbo")).toBeInTheDocument());
    expect(screen.getByText("embedded eng")).toBeInTheDocument();
    expect(screen.getByText("2 muted words in this run")).toBeInTheDocument();
  });

  it("lists detections with their timecode and how they were found", async () => {
    stub();
    render();

    await waitFor(() => expect(screen.getByText("1:33.4")).toBeInTheDocument());
    expect(screen.getByText("4:00.1")).toBeInTheDocument();
    expect(screen.getByText("suspicious")).toBeInTheDocument();
  });

  it("offers both clips and the waveform when they exist", async () => {
    stub();
    render();

    await userEvent.click(
      await screen.findByRole("button", { name: /Review shit at 1:33\.4/ }),
    );

    expect(screen.getByLabelText("Original")).toHaveAttribute(
      "src",
      "/api/media/snippets/job-1/0000/orig.m4a",
    );
    expect(screen.getByLabelText("Clean")).toHaveAttribute(
      "src",
      "/api/media/snippets/job-1/0000/clean.m4a",
    );
    expect(screen.getByRole("img", { name: /Waveform around shit/ })).toBeInTheDocument();
  });

  it("does not offer a player when the clips are gone", async () => {
    stub();
    render();

    await userEvent.click(await screen.findByRole("button", { name: /Review bass at 4:00\.1/ }));
    expect(screen.getByText("No review clips for this detection.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Original")).not.toBeInTheDocument();
  });

  it("whitelists a false positive at the chosen scope and reprocesses", async () => {
    const api = stub(
      {},
      {
        "/items/7/whitelist": {
          id: 9,
          scope: "title",
          scope_id: 3,
          canonical_word: "bass",
          context_text: null,
          created: true,
          job_id: "new-job",
        },
      },
    );
    render();

    await userEvent.click(await screen.findByRole("button", { name: /Review bass/ }));
    await userEvent.selectOptions(
      screen.getByLabelText("Whitelist scope for bass"),
      "title",
    );
    await userEvent.click(screen.getByRole("button", { name: "Whitelist bass" }));

    await waitFor(() =>
      expect(screen.getByText(/“bass” allowed in this title — reprocessing\./)).toBeInTheDocument(),
    );
    expect(api.calls.find((c) => c.path === "/items/7/whitelist")?.body).toEqual({
      canonical_word: "bass",
      scope: "title",
      reprocess: true,
    });
  });

  it("queues a reprocess", async () => {
    const api = stub(
      {},
      {
        "/items/7/actions": {
          action: "reprocess",
          queued: ["j"],
          skipped: {},
          restored: [],
          considered: 1,
          warnings: [],
        },
      },
    );
    render();

    await userEvent.click(await screen.findByRole("button", { name: "Reprocess" }));
    await waitFor(() => expect(screen.getByText("Queued (reprocess).")).toBeInTheDocument());
    expect(api.calls.find((c) => c.path === "/items/7/actions")?.body).toEqual({
      action: "reprocess",
    });
  });

  it("disables Restore original when nothing is backed up", async () => {
    stub();
    render();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Restore original" })).toBeDisabled(),
    );
  });

  it("restores after a confirmation", async () => {
    const api = stub(
      { restorable: true },
      {
        "/items/7/actions": {
          action: "restore",
          queued: [],
          skipped: {},
          restored: [7],
          considered: 1,
          warnings: [],
        },
      },
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render();

    await userEvent.click(await screen.findByRole("button", { name: "Restore original" }));
    await waitFor(() => expect(screen.getByText("Original restored.")).toBeInTheDocument());
    expect(api.calls.find((c) => c.path === "/items/7/actions")?.body).toEqual({
      action: "restore",
    });
  });

  it("lets a whitelist entry be removed", async () => {
    const api = stub({}, { "/whitelist/5": null });
    render();

    await userEvent.click(
      await screen.findByRole("button", { name: "Remove class from the whitelist" }),
    );
    await waitFor(() =>
      expect(api.calls.some((c) => c.method === "DELETE" && c.path === "/whitelist/5")).toBe(true),
    );
  });

  it("offers earlier runs only when there are any", async () => {
    stub();
    render();
    await waitFor(() => expect(screen.queryByText("Runs:")).not.toBeInTheDocument());
  });

  it("shows an earlier run when there is more than one", async () => {
    stub({ jobs: [job({ id: "job-1", state: "done" }), job({ id: "job-0", trigger: "backfill" })] });
    render();

    const summary = await screen.findByText("Runs:");
    expect(within(summary.parentElement as HTMLElement).getAllByRole("button")).toHaveLength(2);
  });

  it("says why the item could not be loaded", async () => {
    mockApi({ routes: {} });
    render();
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });
});
