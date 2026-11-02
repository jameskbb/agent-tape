import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/** Standard page frame: title row (h1 + actions), optional description, content. */
export function Page({
  title,
  description,
  actions,
  breadcrumb,
  children,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumb?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "mx-auto flex w-full max-w-[1320px] flex-col gap-6 px-5 py-6 md:px-8 md:py-8",
        className,
      )}
    >
      <header className="flex flex-col gap-2">
        {breadcrumb ? <div className="text-sm text-muted">{breadcrumb}</div> : null}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold break-words">{title}</h1>
            {description ? <div className="mt-1 max-w-[72ch] text-muted">{description}</div> : null}
          </div>
          {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
        </div>
      </header>
      {children}
    </div>
  );
}
