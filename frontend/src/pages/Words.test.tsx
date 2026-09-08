/**
 * §9.5's Words & Profiles page.
 *
 * The page's job is to make "which words are being muted right now" answerable, so
 * these lean on the two things that are easy to get subtly wrong: the enabled state
 * must be visible per chip (not inferred from a category), and the profile's
 * categories must read as a *separate* control from a word's own switch, because the
 * schema keeps them separate and conflating them would mislead.
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockApi, profile, whitelistEntry, wordList } from "../test/api";
import { renderApp } from "../test/render";
import { WordsPage } from "./Words";

afterEach(() => vi.unstubAllGlobals());

function stub(extra: Record<string, unknown> = {}) {
  return mockApi({
    routes: {
      "/words": wordList(),
      "/profiles": [profile()],
      "/whitelist": [],
      ...extra,
    },
  });
}

describe("the word chips", () => {
  it("shows each word's own enabled state", async () => {
    stub();
    renderApp(<WordsPage />);

    expect(await screen.findByRole("button", { name: "f**k (muted)" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "b****y (not muted)" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("explains why a built-in ships disabled", async () => {
    /** 50 of the 181 shipped entries are off for precision; "why isn't this muted?"
     * should be answerable by hovering the chip rather than by reading the YAML. */
    stub();
    renderApp(<WordsPage />);
    const chip = await screen.findByRole("button", {
      name: "b****y (not muted)",
    });
    expect(chip).toHaveAttribute("title", expect.stringContaining("British English"));
  });

  it("shows a compound's parent and a phrase's focus", async () => {
    stub();
    renderApp(<WordsPage />);
    expect(await screen.findByRole("button", { name: "m**********r (muted)" })).toHaveAttribute(
      "title",
      expect.stringContaining("part of f**k"),
    );
    expect(screen.getByRole("button", { name: "s*n o* a b***h (muted)" })).toHaveAttribute(
      "title",
      expect.stringContaining("mutes only: b***h"),
    );
  });

  it("renders no unmasked word, in visible text or in any label", async () => {
    /** The invariant, asserted in one place: `innerHTML` covers `aria-label`, `title`
     * and `alt` as well as text nodes, which is where masking is easiest to forget.
     * It also covers the page's own hard-coded help copy, which quotes real words. */
    stub({ "/whitelist": [whitelistEntry({ context_text: "thank god" })] });
    renderApp(<WordsPage />);

    await screen.findByRole("button", { name: "f**k (muted)" });
    expect(document.body.innerHTML).not.toMatch(/fuck|bitch|\bgod\b/i);
  });

  it("toggling a word patches just that word", async () => {
    const api = stub();
    renderApp(<WordsPage />);

    await userEvent.click(await screen.findByRole("button", { name: "b****y (not muted)" }));
    await waitFor(() => {
      const patched = api.calls.filter((c) => c.method === "PATCH");
      expect(patched).toHaveLength(1);
      expect(patched[0].path).toBe("/words/3");
      expect(patched[0].body).toEqual({ enabled: true });
    });
  });

  it("offers no delete for a built-in", async () => {
    stub();
    renderApp(<WordsPage />);
    await screen.findByRole("button", { name: "f**k (muted)" });
    expect(screen.queryByRole("button", { name: "Delete f**k" })).not.toBeInTheDocument();
  });

  it("offers a delete for a custom word", async () => {
    stub({
      "/words": wordList({
        words: [
          {
            ...profile(),
            id: 9,
            canonical: "frakking",
            category: "mild",
            forms: ["frakking"],
            is_phrase: false,
            is_builtin: false,
            enabled: true,
            focus: [],
            parent: null,
            note: null,
          },
        ],
      }),
    });
    renderApp(<WordsPage />);
    expect(await screen.findByRole("button", { name: "Delete f******g" })).toBeInTheDocument();
  });

  it('names the profile a category belongs to, rather than saying "this"', async () => {
    /** A word can be enabled and still not muted, because its category is out of the
     * profile — the most confusing thing on this page if it is not said. Naming the
     * profile matters once there is more than one: with the selector showing "Strict",
     * a badge reading "in this profile" is ambiguous about which one it means. */
    stub();
    renderApp(<WordsPage />);
    await screen.findByRole("button", { name: "f**k (muted)" });
    expect(screen.getAllByText("in Default").length).toBeGreaterThan(0);
    expect(screen.getByText("not in Default")).toBeInTheDocument();
  });

  it("the badges follow the profile the editor is showing", async () => {
    /** Found by clicking through against a live backend: the selection used to live
     * inside the editor, so switching profiles left these badges describing the
     * default while the editor described something else. */
    stub({
      "/profiles": [
        profile(),
        profile({ id: 2, name: "Strict", is_default: false, categories: ["mild"] }),
      ],
    });
    renderApp(<WordsPage />);

    await userEvent.selectOptions(await screen.findByLabelText("Profile"), "2");
    // Strict carries `mild` only, so one category is in and the other four are out.
    expect(screen.getByText("in Strict")).toBeInTheDocument();
    expect(screen.getAllByText("not in Strict")).toHaveLength(4);
    expect(screen.queryByText(/in Default/)).not.toBeInTheDocument();
  });
});

describe("adding a word", () => {
  it("posts the canonical, category and forms", async () => {
    const api = stub();
    renderApp(<WordsPage />);

    await userEvent.type(await screen.findByLabelText("Word or phrase"), "frak");
    await userEvent.type(screen.getByLabelText("Other forms, comma separated"), "fraks, fraking");
    await userEvent.selectOptions(screen.getByLabelText("Category"), "mild");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      const posted = api.calls.filter((c) => c.method === "POST" && c.path === "/words");
      expect(posted).toHaveLength(1);
      expect(posted[0].body).toEqual({
        canonical: "frak",
        category: "mild",
        forms: ["fraks", "fraking"],
      });
    });
  });

  it("cannot be submitted empty", async () => {
    stub();
    renderApp(<WordsPage />);
    expect(await screen.findByRole("button", { name: "Add" })).toBeDisabled();
  });

  it("says forms are matched literally", async () => {
    /** §7 forbids generic suffix rules because they yield junk and real false
     * positives; a user typing one word needs to know it is not enough. */
    stub();
    renderApp(<WordsPage />);
    expect(await screen.findByText(/never by\s+suffix rules/)).toBeInTheDocument();
  });
});

describe("the profile editor", () => {
  it("toggling a category patches the profile", async () => {
    const api = stub();
    renderApp(<WordsPage />);

    await userEvent.click(await screen.findByLabelText("mild in Default"));
    await waitFor(() => {
      const patched = api.calls.filter((c) => c.path === "/profiles/1");
      expect(patched).toHaveLength(1);
      expect((patched[0].body as { categories: string[] }).categories).toContain("mild");
    });
  });

  it("removes a category that was on", async () => {
    const api = stub();
    renderApp(<WordsPage />);

    await userEvent.click(await screen.findByLabelText("strong in Default"));
    await waitFor(() => {
      const body = api.calls.find((c) => c.path === "/profiles/1")?.body as {
        categories: string[];
      };
      expect(body.categories).not.toContain("strong");
    });
  });

  it("does not offer to delete the default profile", async () => {
    /** Every title without an override resolves to it, so deleting it would silently
     * change the mute set of the whole library -- the API refuses too. */
    stub();
    renderApp(<WordsPage />);
    await screen.findByText("Default");
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Make default" })).not.toBeInTheDocument();
  });

  it("offers delete and promote for a non-default profile", async () => {
    stub({
      "/profiles": [profile(), profile({ id: 2, name: "Strict", is_default: false })],
    });
    renderApp(<WordsPage />);

    await userEvent.selectOptions(await screen.findByLabelText("Profile"), "2");
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Make default" })).toBeInTheDocument();
  });

  it("warns how many titles a profile is attached to", async () => {
    stub({
      "/profiles": [profile(), profile({ id: 2, name: "Strict", is_default: false, titles: 4 })],
    });
    renderApp(<WordsPage />);
    await userEvent.selectOptions(await screen.findByLabelText("Profile"), "2");
    expect(screen.getByText("4 titles")).toBeInTheDocument();
  });

  it("creates a profile by name", async () => {
    const api = stub();
    renderApp(<WordsPage />);

    await userEvent.type(await screen.findByLabelText("New profile"), "Strict");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => {
      const posted = api.calls.filter((c) => c.method === "POST" && c.path === "/profiles");
      expect(posted).toHaveLength(1);
      expect(posted[0].body).toEqual({ name: "Strict" });
    });
  });

  it("separates the profile's categories from a word's own switch", async () => {
    stub();
    renderApp(<WordsPage />);
    expect(
      await screen.findByText(/Categories are per profile; a word’s own on\/off switch/),
    ).toBeInTheDocument();
  });
});

describe("the whitelist", () => {
  it("says so when nothing is whitelisted", async () => {
    stub();
    renderApp(<WordsPage />);
    expect(await screen.findByText("Nothing whitelisted.")).toBeInTheDocument();
  });

  it("renders each rule with its scope and effect", async () => {
    stub({
      "/whitelist": [
        whitelistEntry({
          id: 1,
          canonical_word: "god",
          context_text: "thank god",
        }),
        whitelistEntry({
          id: 2,
          canonical_word: "god",
          scope: "title",
          scope_id: 3,
          mode: "allow",
          label: "Pluribus",
        }),
      ],
    });
    renderApp(<WordsPage />);

    expect(await screen.findByText("everywhere")).toBeInTheDocument();
    expect(screen.getByText("thank g*d")).toBeInTheDocument();
    expect(screen.getByText("Pluribus")).toBeInTheDocument();
    expect(screen.getByText("leave audible")).toBeInTheDocument();
    expect(screen.getByText("mute anyway")).toBeInTheDocument();
  });

  it("explains that the narrowest scope wins", async () => {
    stub({ "/whitelist": [whitelistEntry()] });
    renderApp(<WordsPage />);
    expect(await screen.findByText(/narrowest scope wins/)).toBeInTheDocument();
  });

  it("removing a rule deletes it", async () => {
    const api = stub({ "/whitelist": [whitelistEntry({ id: 7 })] });
    renderApp(<WordsPage />);

    await userEvent.click(
      await screen.findByRole("button", {
        name: "Remove g*d from the whitelist",
      }),
    );
    await waitFor(() => {
      expect(api.calls.filter((c) => c.method === "DELETE")[0].path).toBe("/whitelist/7");
    });
  });
});

describe("failures", () => {
  it("shows an error rather than an empty page", async () => {
    mockApi({ routes: {}, status: 500 });
    renderApp(<WordsPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
