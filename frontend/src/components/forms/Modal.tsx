import { useId, type ReactNode } from "react";

import { useNativeDialog } from "@/components/ui/Drawer";
import { cn } from "@/lib/cn";

/** A native <dialog> modal (focus trap and Escape come from the platform). */
export function Modal({
  open,
  title,
  description,
  onClose,
  children,
  wide = false,
}: {
  open: boolean;
  title: string;
  description?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const ref = useNativeDialog(open);
  const titleId = useId();
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onClose={onClose}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      className={cn(
        "m-auto rounded-[var(--radius-panel)] border border-line bg-raised p-0 text-ink shadow-[var(--shadow-pop)] backdrop:bg-[rgb(10_16_24/0.45)]",
        wide ? "w-[min(94vw,46rem)]" : "w-[min(94vw,32rem)]",
      )}
    >
      {open ? (
        <div className="flex flex-col gap-4 p-5">
          <div>
            <h2 id={titleId} className="text-lg font-semibold">
              {title}
            </h2>
            {description ? <p className="mt-1 text-sm text-muted">{description}</p> : null}
          </div>
          {children}
        </div>
      ) : null}
    </dialog>
  );
}
