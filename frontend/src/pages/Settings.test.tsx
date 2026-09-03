import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { backups, mockApi } from "../test/api";
import { renderApp } from "../test/render";
import { SettingsPage } from "./Settings";

afterEach(() => vi.unstubAllGlobals());

const SETTINGS = {
  sonarr_url: "http://sonarr:8989",
  sonarr_api_key: "***",
  radarr_url: "",
  radarr_api_key: "",
  jellyfin_url: "",
  jellyfin_api_key: "",
  webhook_token: "***",
  stt_windowed_model: "large-v3-turbo",
  stt_full_model: "medium",
  stt_drift_model: "small",
  stt_full_max_hours: 3,
  cpu_threads: 0,
  beam_size: 2,
  preferred_language: "eng",
  initial_prompt_hint: true,
  vad_filter: false,
  drift_check: true,
  pad_pre_ms: 80,
  pad_post_ms: 120,
  merge_gap_ms: 250,
  fade_edges_ms: 0,
  mute_censored_tokens: true,
  redact_subtitles: true,
  clean_track_lossless: false,
  extra_eac3_downmix: false,
  allow_cross_device_backup: false,
  audit_pass: "idle",
  backup_retention_days: 30,
  mapping_check_delay_s: 90,
  render_parallel: 1,
};

const WEBHOOK = {
  url: "http://vidcleaner:8585/api/webhooks/sonarr",
  header_name: "X-VidCleaner-Token",
  token: "s3cret",
  note: "Paste the header as `Name=Value`.",
};

function stub(extra: Record<string, unknown> = {}) {
  return mockApi({
    routes: {
      "/settings": SETTINGS,
      "/path-mappings": [
        { app: "sonarr", from_prefix: "/tv", to_prefix: "/media/tv" },
      ],
      "/webhooks/setup*": WEBHOOK,
      "/backups": backups(),
      ...extra,
    },
  });
}

describe("settings page", () => {
  it("shows the stored values", async () => {
    stub();
    renderApp(<SettingsPage />);

    await waitFor(() =>
      expect(screen.getByLabelText("Sonarr URL")).toBeDefined(),
    );
    expect(screen.getByDisplayValue("http://sonarr:8989")).toBeInTheDocument();
    expect(screen.getByDisplayValue("large-v3-turbo")).toBeInTheDocument();
    expect(screen.getByText(/a key is stored/)).toBeInTheDocument();
  });

  it("sends only the fields that changed", async () => {
    const api = stub();
    renderApp(<SettingsPage />);

    const field = await screen.findByLabelText(/Pad before/);
    await userEvent.clear(field);
    await userEvent.type(field, "150");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const patch = api.calls.find(
        (c) => c.method === "PATCH" && c.path === "/settings",
      );
      // Sending the whole object would write `***` over the stored Sonarr key.
      expect(patch?.body).toEqual({ pad_pre_ms: 150 });
    });
  });

  it("keeps Save disabled until something changes", async () => {
    stub();
    renderApp(<SettingsPage />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Save changes" }),
      ).toBeDisabled(),
    );
  });

  it("tests an integration with the values on screen", async () => {
    const api = stub({
      "/integrations/sonarr/test": {
        app: "sonarr",
        ok: true,
        version: "4.0.1",
        detail: "",
        latency_ms: 12,
      },
    });
    renderApp(<SettingsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Test Sonarr" }),
    );

    await waitFor(() =>
      expect(screen.getByText(/ok · 4\.0\.1 · 12ms/)).toBeInTheDocument(),
    );
    expect(
      api.calls.find((c) => c.path === "/integrations/sonarr/test")?.body,
    ).toEqual({
      url: "http://sonarr:8989",
      api_key: "***",
    });
  });

  it("reports a failed test rather than throwing it away", async () => {
    stub({
      "/integrations/jellyfin/test": {
        app: "jellyfin",
        ok: false,
        version: null,
        detail: "connection refused",
        latency_ms: 0,
      },
    });
    renderApp(<SettingsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Test Jellyfin" }),
    );
    await waitFor(() =>
      expect(screen.getByText("connection refused")).toBeInTheDocument(),
    );
  });

  it("shows the webhook URL and token to paste", async () => {
    stub();
    renderApp(<SettingsPage />);

    await waitFor(() =>
      expect(screen.getAllByText(WEBHOOK.url).length).toBeGreaterThan(0),
    );
    expect(
      screen.getAllByText(/X-VidCleaner-Token: s3cret/).length,
    ).toBeGreaterThan(0);
  });

  it("installs the webhook in the arr on request", async () => {
    const api = stub({
      "/webhooks/install*": { created: true, id: 3, url: WEBHOOK.url },
    });
    renderApp(<SettingsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Add to Sonarr" }),
    );
    await waitFor(() =>
      expect(screen.getByText("created")).toBeInTheDocument(),
    );
    expect(api.calls.some((c) => c.path.startsWith("/webhooks/install"))).toBe(
      true,
    );
  });

  it("edits and saves path mappings", async () => {
    const api = stub({
      "/path-mappings": [
        { app: "sonarr", from_prefix: "/tv", to_prefix: "/media/tv" },
      ],
    });
    renderApp(<SettingsPage />);

    const theirs = await screen.findByLabelText("Their path 1");
    await userEvent.clear(theirs);
    await userEvent.type(theirs, "/data/tv");
    await userEvent.click(
      screen.getByRole("button", { name: "Save mappings" }),
    );

    await waitFor(() => {
      const put = api.calls.find((c) => c.method === "PUT");
      expect(put?.body).toEqual([
        { app: "sonarr", from_prefix: "/data/tv", to_prefix: "/media/tv" },
      ]);
    });
  });

  it("adds and removes a mapping row", async () => {
    stub();
    renderApp(<SettingsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Add mapping" }),
    );
    expect(screen.getByLabelText("Their path 2")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Remove mapping 2" }),
    );
    expect(screen.queryByLabelText("Their path 2")).not.toBeInTheDocument();
  });
});

describe("the backups panel", () => {
  it("shows what is held and what would be reclaimed", async () => {
    stub();
    renderApp(<SettingsPage />);

    expect(await screen.findByText("3 kept originals")).toBeInTheDocument();
    expect(screen.getByText("6.0 GiB")).toBeInTheDocument();
    expect(screen.getByText("purged after 30 days")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Purge expired \(1\)/ }),
    ).toBeEnabled();
  });

  it("says so when retention is off", async () => {
    stub({
      "/backups": backups({
        summary: { keeps_forever: true, retention_days: 0 },
      }),
    });
    renderApp(<SettingsPage />);
    expect(await screen.findByText("kept forever")).toBeInTheDocument();
  });

  it("cannot purge when there is nothing expired", async () => {
    stub({
      "/backups": backups({ summary: { expired: 0, expired_bytes: 0 } }),
    });
    renderApp(<SettingsPage />);
    expect(
      await screen.findByRole("button", { name: /Purge expired \(0\)/ }),
    ).toBeDisabled();
  });

  it("asks before deleting, and says how much would go", async () => {
    const api = stub();
    renderApp(<SettingsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: /Purge expired/ }),
    );
    expect(
      screen.getByText(
        /Delete 1 original \(2.0 GiB\)\? This cannot be undone\./,
      ),
    ).toBeInTheDocument();
    // Nothing has been sent yet: the confirm is a real gate, not a flourish.
    expect(api.calls.filter((c) => c.path === "/backups/purge")).toHaveLength(
      0,
    );

    await userEvent.click(screen.getByRole("button", { name: "Yes, purge" }));
    await waitFor(() => {
      const posted = api.calls.filter((c) => c.path === "/backups/purge");
      expect(posted).toHaveLength(1);
      expect(posted[0].body).toEqual({ scope: "expired" });
    });
  });

  it("cancelling sends nothing", async () => {
    const api = stub();
    renderApp(<SettingsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: /Purge expired/ }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByText(/cannot be undone/)).not.toBeInTheDocument();
    expect(api.calls.filter((c) => c.path === "/backups/purge")).toHaveLength(
      0,
    );
  });

  it("offers orphaned originals separately, and explains why", async () => {
    stub({
      "/backups": backups({
        summary: { orphaned: 2, orphaned_bytes: 4 * 1024 ** 3 },
      }),
    });
    renderApp(<SettingsPage />);

    expect(
      await screen.findByText(/no longer in the library/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Purge orphaned \(2\)/ }),
    ).toBeEnabled();
  });
});
