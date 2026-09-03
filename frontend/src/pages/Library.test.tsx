import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, title } from "../test/api";
import { renderApp } from "../test/render";
import { LibraryPage } from "./Library";

afterEach(() => vi.unstubAllGlobals());

const WIRE = title();
const SOPRANOS = title({ id: 4, title: "The Sopranos", enabled: false, item_count: 0 });

function stub(extra: Record<string, unknown> = {}) {
  return mockApi({
    routes: { "/library/titles*": { titles: [WIRE, SOPRANOS], total: 2 }, ...extra },
  });
}

describe("library page", () => {
  it("lists titles with their clean progress", async () => {
    stub();
    renderApp(<LibraryPage />);

    await waitFor(() => expect(screen.getByText("1/2 clean")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "The Wire" })).toHaveAttribute("href", "/titles/3");
    expect(screen.getByText("no files")).toBeInTheDocument();
  });

  it("turning on Clean reports how many files it queued", async () => {
    const api = stub({
      "/library/titles/4": { id: 4, enabled: true, profile_id: null, queued: ["a", "b", "c"] },
    });
    renderApp(<LibraryPage />);

    await userEvent.click(await screen.findByRole("checkbox", { name: "Clean The Sopranos" }));

    await waitFor(() =>
      expect(screen.getByText("The Sopranos: queued 3 files")).toBeInTheDocument(),
    );
    const call = api.calls.find((c) => c.method === "PATCH");
    expect(call?.body).toEqual({ enabled: true });
  });

  it("turning Clean off says so and queues nothing", async () => {
    stub({ "/library/titles/3": { id: 3, enabled: false, profile_id: null, queued: [] } });
    renderApp(<LibraryPage />);

    await userEvent.click(await screen.findByRole("checkbox", { name: "Clean The Wire" }));
    await waitFor(() => expect(screen.getByText("The Wire: cleaning off")).toBeInTheDocument());
  });

  it("passes the tab, the search box and the filter to the API", async () => {
    const api = stub();
    renderApp(<LibraryPage />);
    await screen.findByRole("link", { name: "The Wire" });

    await userEvent.click(screen.getByRole("button", { name: "Movies" }));
    await userEvent.type(screen.getByLabelText("Search titles"), "wir");
    await userEvent.click(screen.getByRole("checkbox", { name: /only enabled/i }));

    await waitFor(() => {
      const last = api.calls.filter((c) => c.path.startsWith("/library/titles?")).at(-1);
      expect(last?.path).toContain("kind=movie");
      expect(last?.path).toContain("q=wir");
      expect(last?.path).toContain("enabled=true");
    });
  });

  it("syncs on demand and reports the result", async () => {
    const api = stub({
      "/library/sync": { titles_seen: 12, items_seen: 340, enqueued: ["a", "b"] },
    });
    renderApp(<LibraryPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Sync now" }));

    await waitFor(() =>
      expect(screen.getByText("Sync: 12 titles, 340 files, 2 queued")).toBeInTheDocument(),
    );
    expect(api.calls.some((c) => c.path === "/library/sync")).toBe(true);
  });

  it("points a new user at Settings when the library is empty", async () => {
    mockApi({ routes: { "/library/titles*": { titles: [], total: 0 } } });
    renderApp(<LibraryPage />);
    await waitFor(() => expect(screen.getByText(/Configure Sonarr or Radarr/)).toBeInTheDocument());
  });

  it("shows why a sync failed", async () => {
    mockApi({
      routes: {
        "/library/titles*": { titles: [], total: 0 },
        "/library/sync": () => {
          throw new Error("unused");
        },
      },
    });
    // The stub throws for /library/sync, so the request rejects: the page must say
    // something rather than silently doing nothing.
    renderApp(<LibraryPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });
});
