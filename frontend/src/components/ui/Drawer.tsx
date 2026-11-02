import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";

import { cn } from "@/lib/cn";

import { IconButton } from "./Button";

/** Open or close a native <dialog> as ``open`` changes (focus trap and Escape come from
 * the platform; older engines without ``showModal`` get the ``open`` attribute). */
export function useNativeDialog(open: boolean): RefObject<HTMLDialogElement | null> {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      if (typeof d.showModal === "function") d.showModal();
      else d.setAttribute("open", "");
    } else if (!open && d.open) {
      if (typeof d.close === "function") d.close();
      else d.removeAttribute("open");
    }
  }, [open]);
  return ref;
}

/** A modal side panel anchored to the right edge, with a titled header and a close button.
 * The body renders only while open. */
export function Drawer({
  open,
  eyebrow,
  title,
  closeLabel = "Close",
  onClose,
  wide = false,
  children,
}: {
  open: boolean;
  /** Small caption above the title ("Context source", "File evidence"). */
  eyebrow?: ReactNode;
  title: ReactNode;
  closeLabel?: string;
  onClose: () => void;
  wide?: boolean;
  children: ReactNode;
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
        "m-0 ml-auto h-dvh max-h-dvh max-w-none border-l border-line bg-raised p-0 text-ink shadow-[var(--shadow-pop)] backdrop:bg-[rgb(10_16_24/0.45)]",
        wide ? "w-[min(100vw,60rem)]" : "w-[min(100vw,34rem)]",
      )}
    >
      {open ? (
        <div className="flex h-full flex-col">
          <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
            <div className="min-w-0">
              {eyebrow ? <p className="text-xs text-muted">{eyebrow}</p> : null}
              <h2 id={titleId} className="text-base font-semibold break-words">
                {title}
              </h2>
            </div>
            <IconButton
              label={closeLabel}
              icon={<X className="size-4" aria-hidden />}
              onClick={onClose}
            />
          </header>
          <div className="flex-1 overflow-y-auto p-4">{children}</div>
        </div>
      ) : null}
    </dialog>
  );
}
