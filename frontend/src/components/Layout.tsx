import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

/** The six pages of PLAN.md §9. Only Queue is wired to the API in M0. */
const NAV = [
  { to: "/", label: "Queue", end: true },
  { to: "/library", label: "Library", end: false },
  { to: "/words", label: "Words & Profiles", end: false },
  { to: "/settings", label: "Settings", end: false },
];

/**
 * One nav element, always mounted, appearing once in the DOM: below `md` it is an off-canvas
 * drawer, at `md` and up it is the static rail it has always been. The two are deliberately the
 * same element -- a separate mobile nav would put every link in the document twice, and a review
 * screen's `getByRole("link", { name })` throws on two hits.
 *
 * The closed drawer is hidden with `invisible`, not `aria-hidden` or `hidden`: `visibility`
 * drops its links from tab order and the a11y tree through CSS alone, so no breakpoint ever
 * reaches JavaScript. It still animates only because `visibility` is in the transition list --
 * an interpolating `hidden <-> visible` computes to `visible` throughout, so the drawer stays
 * painted for the whole slide out. Drop it and closing teleports.
 *
 * The transitioned property is `translate`, not `transform`: Tailwind v4's `-translate-x-full`
 * sets the independent `translate` property, so `transition-[transform,…]` animates nothing
 * and the drawer jumps while only its visibility fades.
 */
export function Layout() {
  const [open, setOpen] = useState(false);
  const location = useLocation();

  // Close on navigation. Keyed on `key`, not `pathname`, so tapping the link for the page you
  // are already on still closes the drawer.
  useEffect(() => setOpen(false), [location.key]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      {/* Sticky rather than fixed, so it keeps a flow box and `main` needs no top offset. */}
      <header className="sticky top-0 z-20 flex items-center gap-2 border-b border-slate-800 bg-slate-950/90 px-2 py-1.5 backdrop-blur md:hidden">
        <button
          type="button"
          aria-label="Menu"
          aria-expanded={open}
          aria-controls="app-nav"
          onClick={() => setOpen((value) => !value)}
          className="inline-flex h-11 w-11 items-center justify-center rounded text-xl leading-none text-slate-300 hover:bg-slate-800 hover:text-slate-100"
        >
          ☰
        </button>
        <span className="text-base font-semibold text-slate-100">VidCleaner</span>
      </header>

      <nav
        id="app-nav"
        className={`fixed inset-y-0 left-0 z-40 w-56 shrink-0 overflow-y-auto border-r border-slate-800 bg-slate-900 p-4 transition-[translate,visibility] duration-200 ease-out motion-reduce:transition-none md:static md:z-auto md:visible md:translate-x-0 md:overflow-y-visible md:bg-slate-900/60 md:transition-none ${
          open ? "visible translate-x-0" : "invisible -translate-x-full"
        }`}
      >
        <div className="mb-6 px-2">
          <div className="text-lg font-semibold text-slate-100">VidCleaner</div>
          <div className="text-xs text-slate-500">profanity muting for Jellyfin</div>
        </div>
        <ul className="space-y-1">
          {NAV.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `block rounded px-3 py-2 text-sm ${
                    isActive
                      ? "bg-slate-800 text-slate-100"
                      : "text-slate-400 hover:bg-slate-800/50 hover:text-slate-200"
                  }`
                }
              >
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      {/* A real button rather than an aria-hidden div: it costs nothing, and it is the only
          close control a keyboard user has, since there is no focus trap. */}
      {open && (
        <button
          type="button"
          aria-label="Close menu"
          onClick={() => setOpen(false)}
          className="fixed inset-0 z-30 bg-slate-950/60 md:hidden"
        />
      )}

      <main className="min-w-0 flex-1 p-4 sm:p-6 md:p-8">
        <Outlet />
      </main>
    </div>
  );
}
