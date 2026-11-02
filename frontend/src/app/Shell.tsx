import { Menu, Monitor, Moon, Sun, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet } from "react-router";

import { ReelMark } from "@/components/layout/ReelMark";
import { cn } from "@/lib/cn";

import { navEntries } from "./registry";
import { useTheme, type ThemeMode } from "./theme";

const THEME_OPTIONS: { mode: ThemeMode; label: string; Icon: typeof Sun }[] = [
  { mode: "system", label: "System theme", Icon: Monitor },
  { mode: "light", label: "Light theme", Icon: Sun },
  { mode: "dark", label: "Dark theme", Icon: Moon },
];

export function ThemeSwitch() {
  const { mode, setMode } = useTheme();
  return (
    <div
      role="radiogroup"
      aria-label="Colour theme"
      className="inline-flex rounded-[var(--radius-control)] border border-line bg-sunken p-0.5"
    >
      {THEME_OPTIONS.map(({ mode: m, label, Icon }) => (
        <button
          key={m}
          type="button"
          role="radio"
          aria-checked={mode === m}
          aria-label={label}
          title={label}
          onClick={() => setMode(m)}
          className={cn(
            "inline-flex size-7 items-center justify-center rounded-[4px] text-muted",
            mode === m && "bg-surface text-ink shadow-sm",
          )}
        >
          <Icon className="size-3.5" aria-hidden />
        </button>
      ))}
    </div>
  );
}

function Nav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Main" className="flex flex-col gap-0.5">
      {navEntries().map(({ id, label, icon: Icon, path, end }) => (
        <NavLink
          key={id}
          to={path}
          end={end}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "group relative flex items-center gap-2.5 rounded-[var(--radius-control)] px-2.5 py-1.5 text-[0.9375rem] transition-colors",
              isActive
                ? "bg-surface font-medium text-ink shadow-[0_0_0_1px_var(--line)]"
                : "text-ink-soft hover:bg-surface/60 hover:text-ink",
            )
          }
        >
          {({ isActive }) => (
            <>
              <span
                aria-hidden
                className={cn(
                  "absolute top-1.5 bottom-1.5 left-0 w-0.5 rounded-full",
                  isActive ? "bg-accent" : "bg-transparent",
                )}
              />
              <Icon
                aria-hidden
                className={cn(
                  "size-4",
                  isActive ? "text-accent" : "text-faint group-hover:text-muted",
                )}
              />
              {label}
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-2.5 px-1">
      <ReelMark className="size-7" />
      <span className="text-lg font-semibold tracking-tight">AgentTape</span>
    </div>
  );
}

/**
 * Navigation drawer below the lg breakpoint. A native modal <dialog>: the platform traps
 * focus and makes the page inert; Escape and a backdrop click close it; focus returns to
 * the menu button. It closes itself if the window grows to the desktop layout.
 */
function NavDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
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
  useEffect(() => {
    if (!open || typeof matchMedia !== "function") return;
    const mq = matchMedia("(min-width: 1024px)");
    const onChange = () => {
      if (mq.matches) onClose();
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [open, onClose]);
  return (
    <dialog
      ref={ref}
      aria-label="Navigation"
      onClose={onClose}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose(); // backdrop click
      }}
      onKeyDown={(e) => {
        // Keep Tab inside the drawer (browsers otherwise let it reach their own toolbar).
        if (e.key !== "Tab") return;
        const items = Array.from(
          e.currentTarget.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        );
        const first = items[0];
        const last = items.at(-1);
        if (!first || !last) return;
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }}
      className="m-0 h-full max-h-none w-64 max-w-[85vw] border-0 bg-sunken p-0 text-ink shadow-[var(--shadow-pop)] backdrop:bg-[rgb(10_16_24/0.45)]"
    >
      {open ? (
        <div className="flex h-full flex-col gap-6 px-3 py-4">
          <div className="flex items-center justify-between">
            <Brand />
            <button
              type="button"
              aria-label="Close navigation"
              autoFocus
              onClick={onClose}
              className="inline-flex size-8 items-center justify-center rounded text-muted hover:text-ink"
            >
              <X className="size-4" aria-hidden />
            </button>
          </div>
          <Nav onNavigate={onClose} />
          <div className="mt-auto flex items-center justify-between px-1">
            <span className="text-xs text-faint">Theme</span>
            <ThemeSwitch />
          </div>
        </div>
      ) : null}
    </dialog>
  );
}

export function Shell() {
  const [open, setOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeDrawer = useCallback(() => {
    setOpen(false);
    menuButton.current?.focus();
  }, []);
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[228px_minmax(0,1fr)]">
      <a
        href="#main"
        className="sr-only z-50 rounded bg-surface px-3 py-2 focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Skip to content
      </a>
      <aside className="sticky top-0 hidden h-screen flex-col gap-6 border-r border-line bg-sunken px-3 py-4 lg:flex">
        <Brand />
        <Nav />
        <div className="mt-auto flex items-center justify-between px-1">
          <span className="text-xs text-faint">Theme</span>
          <ThemeSwitch />
        </div>
      </aside>
      <div className="sticky top-0 z-30 flex items-center justify-between border-b border-line bg-sunken px-4 py-2 lg:hidden">
        <Brand />
        <button
          ref={menuButton}
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Open navigation"
          aria-haspopup="dialog"
          aria-expanded={open}
          className="inline-flex size-9 items-center justify-center rounded-[var(--radius-control)] text-ink hover:bg-surface"
        >
          <Menu className="size-5" aria-hidden />
        </button>
      </div>
      <NavDrawer open={open} onClose={closeDrawer} />
      <main id="main" tabIndex={-1} className="min-w-0 focus:outline-none">
        <Outlet />
      </main>
    </div>
  );
}
