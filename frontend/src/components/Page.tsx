import type { ReactNode } from "react";

export function Page({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children?: ReactNode;
}) {
  return (
    <section>
      <h1 className="text-2xl font-semibold text-slate-100">{title}</h1>
      {subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}
      <div className="mt-6">{children}</div>
    </section>
  );
}

/** Marks a page whose content arrives in a later milestone. */
export function Placeholder({ milestone, children }: { milestone: string; children: ReactNode }) {
  return (
    <div className="rounded border border-dashed border-slate-700 p-6 text-sm text-slate-400">
      <div className="mb-1 font-medium text-slate-300">Arrives in {milestone}</div>
      {children}
    </div>
  );
}
