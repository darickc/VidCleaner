import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { EMPTY_QUEUE, HEALTH, mockApi, profile, wordList } from "./test/api";
import { renderApp } from "./test/render";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("app shell", () => {
  it("renders the navigation for every §9 page", () => {
    mockApi({ routes: { "/health": HEALTH, "/jobs": EMPTY_QUEUE } });
    renderApp(<App />);
    for (const label of ["Queue", "Library", "Words & Profiles", "Settings"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("shows live health on the queue page", async () => {
    mockApi({ routes: { "/health": HEALTH, "/jobs": EMPTY_QUEUE } });
    renderApp(<App />);
    await waitFor(() => expect(screen.getByText("degraded")).toBeInTheDocument());
    expect(screen.getByText(/v0\.1\.0/)).toBeInTheDocument();
    expect(screen.getByText(/migration 0001/)).toBeInTheDocument();
    expect(screen.getByText(/ffmpeg is not installed/)).toBeInTheDocument();
  });

  it("reports a failure instead of rendering stale health", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 })),
    );
    renderApp(<App />);
    await waitFor(() =>
      expect(screen.getAllByText("Could not reach the API.").length).toBeGreaterThan(0),
    );
  });

  it("routes to the words page, which is no longer a placeholder", async () => {
    mockApi({ routes: { "/words": wordList(), "/profiles": [profile()], "/whitelist": [] } });
    renderApp(<App />, { route: "/words" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "fuck (muted)" })).toBeInTheDocument(),
    );
    expect(screen.queryByText(/M5/)).not.toBeInTheDocument();
  });

  it("routes to the settings page", async () => {
    mockApi({
      routes: {
        "/settings": { audit_pass: "idle", sonarr_url: "", sonarr_api_key: "***" },
        "/path-mappings": [],
      },
    });
    renderApp(<App />, { route: "/settings" });
    await waitFor(() => expect(screen.getByText("Integrations")).toBeInTheDocument());
  });
});
