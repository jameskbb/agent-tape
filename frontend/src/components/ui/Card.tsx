import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface CardProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  padded?: boolean;
}

/** A bordered panel. Title renders as an h2 so pages keep a sensible outline. */
export function Card({
  title,
  description,
  actions,
  padded = true,
  className,
  children,
  ...rest
}: CardProps) {
  return (
    <section
      className={cn("rounded-[var(--radius-panel)] border border-line bg-surface", className)}
      {...rest}
    >
      {(title ?? actions) ? (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0">
            {title ? <h2 className="text-base font-semibold">{title}</h2> : null}
            {description ? <p className="mt-0.5 text-sm text-muted">{description}</p> : null}
          </div>
          {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
        </header>
      ) : null}
      <div className={cn(padded && "p-4")}>{children}</div>
    </section>
  );
}
