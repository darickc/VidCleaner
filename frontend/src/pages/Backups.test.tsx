import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { backupRow, backups, mockApi } from "../test/api";
import { renderApp } from "../test/render";
import { BackupsPage } from "./Backups";

afterEach(() => vi.unstubAllGlobals());

const KEPT = backupRow();
const ORPHAN = backupRow({
  id: 2,
  media_item_id: 99,
  label: "item 99",
  original_path: "",
  rel_path: "movies/Gone (2011)/Gone.mkv",
  identified: false,
  state: "orphaned",
  size: 40 * 1024 ** 3,
});

const stub = (routes: Record<string, unknown> = {}) =>
  mockApi({
    routes: {
      "/backups*": backups({ backups: [KEPT, ORPHAN] }),
      ...routes,
    },
  });

describe("the backups list", () => {
  it("names each original by its path under the backups directory", async () => {
    stub();
    renderApp(<BackupsPage />);

    // The backups tree mirrors the library tree, which is what makes this readable
    // without the database -- and it is the only name an adopted orphan has.
    expect(await screen.findByText("tv/Show/S01E01.mkv")).toBeInTheDocument();
    expect(screen.getByText("movies/Gone (2011)/Gone.mkv")).toBeInTheDocument();
  });

  it("links an identified backup to its item and an orphan to nothing", async () => {
    stub();
    renderApp(<BackupsPage />);

    expect(await screen.findByRole("link", { name: "Show S01E01" })).toHaveAttribute(
      "href",
      "/items/1",
    );
    expect(screen.getByText("no matching item")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "item 99" })).not.toBeInTheDocument();
  });

  it("explains why orphans cannot be restored", async () => {
    stub({ "/backups*": backups({ summary: { orphaned: 1, orphaned_bytes: 4 } }) });
    renderApp(<BackupsPage />);

    expect(
      await screen.findByText(/no longer in the library/),
    ).toBeInTheDocument();
  });

  it("filters to orphans only", async () => {
    const api = stub();
    renderApp(<BackupsPage />);
    await screen.findByText("tv/Show/S01E01.mkv");

    await userEvent.click(screen.getByRole("button", { name: "Orphaned" }));

    await waitFor(() => {
      expect(
        api.calls.some((c) => c.path.includes("state=orphaned")),
      ).toBe(true);
    });
  });

  it("asks the server for the biggest originals first", async () => {
    const api = stub();
    renderApp(<BackupsPage />);
    await screen.findByText("tv/Show/S01E01.mkv");

    await userEvent.selectOptions(
      screen.getByLabelText("Sort backups"),
      "largest",
    );

    await waitFor(() => {
      expect(api.calls.some((c) => c.path.includes("sort=largest"))).toBe(true);
    });
  });
});

describe("acting on one row", () => {
  it("confirms before deleting a single original, and says how big it is", async () => {
    const api = stub({
      "/backups/2": { purged: 1, freed_bytes: 40 * 1024 ** 3, missing: 0, warnings: [] },
    });
    renderApp(<BackupsPage />);
    await screen.findByText("movies/Gone (2011)/Gone.mkv");

    await userEvent.click(screen.getAllByRole("button", { name: "Delete" })[1]);
    // Twice: once in its row, once in a banner outside the table naming what is
    // about to go. Inside the table the confirm sits in a horizontally scrolling
    // container, where on a phone it is off the right-hand edge.
    expect(screen.getAllByText("movies/Gone (2011)/Gone.mkv")).toHaveLength(2);
    expect(screen.getByText(/\(40.0 GiB\)\? This cannot be undone\./)).toBeInTheDocument();
    expect(api.calls.filter((c) => c.method === "DELETE")).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: "Yes, delete" }));
    await waitFor(() => {
      const sent = api.calls.filter((c) => c.method === "DELETE");
      expect(sent).toHaveLength(1);
      expect(sent[0].path).toBe("/backups/2");
    });
  });

  it("cancelling sends nothing", async () => {
    const api = stub();
    renderApp(<BackupsPage />);
    await screen.findByText("tv/Show/S01E01.mkv");

    await userEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByText(/cannot be undone/)).not.toBeInTheDocument();
    expect(api.calls.filter((c) => c.method === "DELETE")).toHaveLength(0);
  });

  it("offers Restore only where there is something to restore into", async () => {
    stub();
    renderApp(<BackupsPage />);
    await screen.findByText("tv/Show/S01E01.mkv");

    // One button for the kept, identified row; none for the orphan, whose library
    // file is gone, and none for a `restored` row whose file is already back.
    expect(screen.getAllByRole("button", { name: "Restore" })).toHaveLength(1);
  });

  it("restores through the item action, so sidecars go back with the video", async () => {
    const api = stub({
      "/items/1/actions": { action: "restore", restored: [1], warnings: [] },
    });
    renderApp(<BackupsPage />);
    await screen.findByText("tv/Show/S01E01.mkv");

    await userEvent.click(screen.getByRole("button", { name: "Restore" }));

    await waitFor(() => {
      const sent = api.calls.filter((c) => c.path === "/items/1/actions");
      expect(sent).toHaveLength(1);
      expect(sent[0].body).toEqual({ action: "restore" });
    });
  });

  it("marks a row whose file has gone missing", async () => {
    stub({
      "/backups*": backups({ backups: [backupRow({ exists: false })] }),
    });
    renderApp(<BackupsPage />);

    expect(await screen.findByText("file gone")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore" })).toBeDisabled();
  });
});

describe("the bulk actions", () => {
  it("asks before purging everything expired, and says how much would go", async () => {
    const api = stub({
      "/backups/purge": {
        purged: 1,
        freed_bytes: 2 * 1024 ** 3,
        missing: 0,
        warnings: [],
      },
    });
    renderApp(<BackupsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: /Purge expired \(1\)/ }),
    );
    expect(
      screen.getByText(/Delete 1 original \(2.0 GiB\)\? This cannot be undone\./),
    ).toBeInTheDocument();
    expect(api.calls.filter((c) => c.path === "/backups/purge")).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: "Yes, purge" }));
    await waitFor(() => {
      const posted = api.calls.filter((c) => c.path === "/backups/purge");
      expect(posted).toHaveLength(1);
      expect(posted[0].body).toEqual({ scope: "expired" });
    });
  });

  it("cannot purge when there is nothing expired", async () => {
    stub({
      "/backups*": backups({ summary: { expired: 0, expired_bytes: 0 } }),
    });
    renderApp(<BackupsPage />);
    expect(
      await screen.findByRole("button", { name: /Purge expired \(0\)/ }),
    ).toBeDisabled();
  });

  it("rescans the directory, so the list is not an hour old", async () => {
    stub({
      "/backups/reconcile": { adopted: 2, purged: 0, skipped: false, note: "" },
    });
    renderApp(<BackupsPage />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Rescan directory" }),
    );

    expect(await screen.findByText(/Adopted 2 untracked file/)).toBeInTheDocument();
  });
});

describe("the hidden-directory notice", () => {
  it("says so when the originals are still somewhere invisible", async () => {
    stub({
      "/backups*": backups({
        summary: {
          backups_dir: "/media/.vidcleaner-backups",
          backups_dir_is_hidden: true,
        },
      }),
    });
    renderApp(<BackupsPage />);

    expect(
      await screen.findByText(/will not show up while you are/),
    ).toBeInTheDocument();
  });

  it("stays quiet once it is visible", async () => {
    stub();
    renderApp(<BackupsPage />);
    await screen.findByText("tv/Show/S01E01.mkv");

    expect(screen.queryByText(/will not show up/)).not.toBeInTheDocument();
  });
});
