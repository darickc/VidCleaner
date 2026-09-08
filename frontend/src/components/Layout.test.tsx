/**
 * The shell's drawer, asserted through DOM and ARIA only.
 *
 * jsdom applies no CSS -- `index.css` is imported by `main.tsx`, which no test imports -- so
 * these cannot check that the closed drawer is off-screen: `toBeVisible()` reports it visible
 * in both states, because `invisible` never becomes a computed style. Asserting the raw class
 * string instead would pin Tailwind's output rather than any behaviour. The transform and
 * visibility half is verified in a browser; what is verified here is the state machine, and
 * above all that each nav link exists exactly once.
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { EMPTY_QUEUE, HEALTH, mockApi, title } from "../test/api";
import { renderApp } from "../test/render";

afterEach(() => {
  vi.unstubAllGlobals();
});

const NAV_LABELS = ["Queue", "Library", "Words & Profiles", "Settings"];

function shell(extra: Record<string, unknown> = {}) {
  return mockApi({
    routes: {
      "/health": HEALTH,
      "/jobs": EMPTY_QUEUE,
      "/library/titles*": { titles: [title()], total: 1 },
      ...extra,
    },
  });
}

const menu = () => screen.getByRole("button", { name: "Menu" });
const scrim = () => screen.queryByRole("button", { name: "Close menu" });

describe("app shell drawer", () => {
  it("renders each nav link exactly once", () => {
    shell();
    renderApp(<App />);
    // The regression guard for the whole design: one nav element serves both the mobile
    // drawer and the desktop rail, so a second mobile-only nav would break every
    // `getByRole("link")` in the suite.
    for (const label of NAV_LABELS) {
      expect(screen.getAllByRole("link", { name: label })).toHaveLength(1);
    }
  });

  it("starts collapsed, pointing at the nav it actually controls", () => {
    shell();
    renderApp(<App />);
    expect(menu()).toHaveAttribute("aria-expanded", "false");
    expect(menu()).toHaveAttribute("aria-controls", "app-nav");
    // Not just any id: the one the nav really has, so the attribute cannot go stale.
    expect(document.getElementById("app-nav")).toBe(screen.getByRole("navigation"));
    expect(scrim()).not.toBeInTheDocument();
  });

  it("opens on the hamburger", async () => {
    shell();
    renderApp(<App />);
    await userEvent.click(menu());
    expect(menu()).toHaveAttribute("aria-expanded", "true");
    expect(scrim()).toBeInTheDocument();
  });

  it("closes on the scrim", async () => {
    shell();
    renderApp(<App />);
    await userEvent.click(menu());
    await userEvent.click(scrim()!);
    expect(menu()).toHaveAttribute("aria-expanded", "false");
    expect(scrim()).not.toBeInTheDocument();
  });

  it("closes on Escape, and ignores it while closed", async () => {
    shell();
    renderApp(<App />);
    // Closed: the listener is not mounted at all, so this must be inert rather than a toggle.
    await userEvent.keyboard("{Escape}");
    expect(menu()).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(menu());
    await userEvent.keyboard("{Escape}");
    expect(menu()).toHaveAttribute("aria-expanded", "false");
  });

  it("closes when a link navigates away", async () => {
    shell();
    renderApp(<App />);
    await userEvent.click(menu());
    await userEvent.click(screen.getByRole("link", { name: "Library" }));
    await waitFor(() => expect(menu()).toHaveAttribute("aria-expanded", "false"));
    expect(scrim()).not.toBeInTheDocument();
  });

  it("closes when the link is for the page you are already on", async () => {
    shell();
    renderApp(<App />);
    await userEvent.click(menu());
    await userEvent.click(screen.getByRole("link", { name: "Queue" }));
    // Pins the effect to `location.key`: keyed on `pathname` this navigation is a no-op and
    // the drawer stays open over the page it just "went" to.
    await waitFor(() => expect(menu()).toHaveAttribute("aria-expanded", "false"));
  });
});
