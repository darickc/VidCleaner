import { NavLink, Outlet } from "react-router-dom";

/** The six pages of PLAN.md §9. Only Queue is wired to the API in M0. */
const NAV = [
  { to: "/", label: "Queue", end: true },
  { to: "/library", label: "Library", end: false },
  { to: "/words", label: "Words & Profiles", end: false },
  { to: "/settings", label: "Settings", end: false },
];

export function Layout() {
  return (
    <div className="flex min-h-screen">
      <nav className="w-56 shrink-0 border-r border-slate-800 bg-slate-900/60 p-4">
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
      <main className="flex-1 p-8">
        <Outlet />
      </main>
    </div>
  );
}
