import { useId, useState, type ReactNode } from "react";

import { Button } from "./Button";
import { useNativeDialog } from "./Drawer";

/**
 * Confirmation for material actions, built on the native <dialog> element (focus trap,
 * Escape to close). ``onConfirm`` receives the optional note when ``withNote`` is set.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  tone = "primary",
  withNote = false,
  notePlaceholder,
  busy = false,
  error,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger";
  withNote?: boolean;
  notePlaceholder?: string;
  busy?: boolean;
  /** Shown inline above the buttons when the last confirm attempt failed; the dialog stays
   * open (the caller does not close it on error) so the reason is visible next to the note. */
  error?: ReactNode;
  onConfirm: (note: string) => void;
  onClose: () => void;
}) {
  const ref = useNativeDialog(open);
  const [note, setNote] = useState("");
  const titleId = useId();
  // The native close event fires however the dialog closes; the next opening starts blank.
  const close = () => {
    setNote("");
    onClose();
  };
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onClose={close}
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
      className="m-auto w-[min(92vw,28rem)] rounded-[var(--radius-panel)] border border-line bg-raised p-0 text-ink shadow-[var(--shadow-pop)] backdrop:bg-[rgb(10_16_24/0.45)]"
    >
      <form
        method="dialog"
        className="flex flex-col gap-4 p-5"
        onSubmit={(e) => {
          e.preventDefault();
          onConfirm(note);
        }}
      >
        <h2 id={titleId} className="text-lg font-semibold">
          {title}
        </h2>
        {description ? <div className="text-sm text-ink-soft">{description}</div> : null}
        {withNote ? (
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted">Note (optional, saved to the audit log)</span>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={notePlaceholder}
              rows={3}
              className="rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-2 text-base text-ink"
            />
          </label>
        ) : null}
        {error ? (
          <p role="alert" className="text-sm text-[var(--st-failed)]">
            {error}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose} disabled={busy}>
            Keep as is
          </Button>
          <Button type="submit" variant={tone === "danger" ? "danger" : "primary"} loading={busy}>
            {confirmLabel}
          </Button>
        </div>
      </form>
    </dialog>
  );
}
