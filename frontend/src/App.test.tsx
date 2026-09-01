import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { renderApp } from "./test/render";

const HEALTH = {
  status: "degraded",
  version: "0.1.0",
  role: "all",
  database: { ok: true, error: null, revision: "0001" },
  ffmpeg: { present: false, version: null, path: null },
  disk: {
    config: { path: "/config", exists: true, free_bytes: 2 ** 30, total_bytes: 2 ** 40 },
    media: { path: "/media", exists: true, free_bytes: 2 ** 30, total_bytes: 2 ** 40 },
    backups: { path: "/backups", exists: true, free_bytes: 2 ** 30, total_bytes: 2 ** 40 },
    work: { path: "/work", exists: true, free_bytes: 2 ** 30, total_bytes: 2 ** 40 },
  },
};

function mockApi(payload: unknown = HEALTH) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("app shell", () => {
  it("renders the navigation for every §9 page", () => {
    mockApi();
    renderApp(<App />);
    for (const label of ["Queue", "Library", "Words & Profiles", "Settings"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("shows live health on the queue page", async () => {
    mockApi();
    renderApp(<App />);
    await waitFor(() => expect(screen.getByText("degraded")).toBeInTheDocument());
    expect(screen.getByText("0.1.0")).toBeInTheDocument();
    expect(screen.getByText("not installed")).toBeInTheDocument();
    expect(screen.getByText("migration 0001")).toBeInTheDocument();
  });

  it("reports a failure instead of rendering stale health", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 })),
    );
    renderApp(<App />);
    await waitFor(() =>
      expect(screen.getByText("Could not reach the API.")).toBeInTheDocument(),
    );
  });

  it("routes to the settings page", async () => {
    mockApi({ audit_pass: "idle", sonarr_api_key: "***" });
    renderApp(<App />, { route: "/settings" });
    await waitFor(() => expect(screen.getByText("audit_pass")).toBeInTheDocument());
  });
});
