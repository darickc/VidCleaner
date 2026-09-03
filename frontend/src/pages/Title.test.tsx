import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { item, mockApi, title } from "../test/api";
import { renderApp } from "../test/render";
import { TitlePage } from "./Title";

afterEach(() => vi.unstubAllGlobals());

const DETAIL = {
  title: title(),
  profile_name: null,
  items: [
    { ...item(), detection_count: 4 },
    {
      ...item({ id: 8, episode: 2, episode_title: "The Detail", status: "failed" }),
      detection_count: 0,
    },
  ],
  counts: [
    { word_canonical: "shit", category: "strong", total: 3, muted: 3 },
    { word_canonical: "damn", category: "religious", total: 1, muted: 1 },
  ],
};

function stub(extra: Record<string, unknown> = {}) {
  return mockApi({ routes: { "/library/titles/3": DETAIL, ...extra } });
}

function render() {
  return renderApp(<TitlePage />, { route: "/titles/3" });
}

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useParams: () => ({ titleId: "3" }) };
});

describe("title page", () => {
  it("lists the episodes with status and detection counts", async () => {
    stub();
    render();

    await waitFor(() => expect(screen.getByText("S01E01")).toBeInTheDocument());
    expect(screen.getByText("4 muted")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "S01E01" })).toHaveAttribute("href", "/items/7");
  });

  it("shows the per-word rollup", async () => {
    stub();
    render();
    await waitFor(() => expect(screen.getByText("shit")).toBeInTheDocument());
    expect(screen.getByText("3×")).toBeInTheDocument();
    expect(screen.getByText("religious")).toBeInTheDocument();
  });

  it("processes every file and reports what was queued", async () => {
    const api = stub({
      "/library/titles/3/actions": {
        action: "process",
        queued: ["a", "b"],
        skipped: {},
        restored: [],
        considered: 2,
        warnings: [],
      },
    });
    render();

    await userEvent.click(await screen.findByRole("button", { name: "Process now" }));

    await waitFor(() => expect(screen.getByText("process: queued 2")).toBeInTheDocument());
    expect(api.calls.find((c) => c.method === "POST")?.body).toEqual({ action: "process" });
  });

  it("explains why files were skipped", async () => {
    stub({
      "/library/titles/3/actions": {
        action: "process",
        queued: [],
        skipped: { already_active: 2 },
        restored: [],
        considered: 2,
        warnings: [],
      },
    });
    render();

    await userEvent.click(await screen.findByRole("button", { name: "Process now" }));
    await waitFor(() =>
      expect(screen.getByText("process: 2 skipped (already active)")).toBeInTheDocument(),
    );
  });

  it("asks before reprocessing everything", async () => {
    const api = stub();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render();

    await userEvent.click(await screen.findByRole("button", { name: "Reprocess all" }));
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("asks before restoring the originals", async () => {
    const api = stub({
      "/library/titles/3/actions": {
        action: "restore",
        queued: [],
        skipped: {},
        restored: [7],
        considered: 2,
        warnings: ["sonarr rescan failed: timeout"],
      },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render();

    await userEvent.click(await screen.findByRole("button", { name: "Restore originals" }));
    await waitFor(() => expect(screen.getByText(/restored 1/)).toBeInTheDocument());
    expect(screen.getByText(/sonarr rescan failed/)).toBeInTheDocument();
    expect(api.calls.find((c) => c.method === "POST")?.body).toEqual({ action: "restore" });
  });

  it("toggles cleaning for the title", async () => {
    const api = stub({ "/library/titles/3": DETAIL });
    render();

    await userEvent.click(await screen.findByRole("checkbox", { name: "Clean The Wire" }));
    await waitFor(() =>
      expect(api.calls.find((c) => c.method === "PATCH")?.body).toEqual({ enabled: false }),
    );
  });
});
