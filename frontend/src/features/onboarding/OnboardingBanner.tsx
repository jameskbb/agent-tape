import { Compass, X } from "lucide-react";
import { Link } from "react-router";

import { describeError } from "@/api/client";
import { IconButton, useToast } from "@/components/ui";

import { useOnboarding, useOnboardingAction } from "./api";

/** Shown on the Overview until the first-run setup is completed or dismissed. */
export function OnboardingBanner() {
  const state = useOnboarding();
  const action = useOnboardingAction();
  const toast = useToast();
  if (!state.data?.show) return null;
  return (
    <aside
      aria-label="Set up AgentTape"
      className="flex flex-wrap items-center gap-3 rounded-[var(--radius-panel)] border border-[color-mix(in_srgb,var(--accent)_35%,transparent)] bg-accent-soft px-4 py-3"
    >
      <Compass aria-hidden className="size-5 shrink-0 text-accent" />
      <p className="min-w-0 flex-1 text-sm text-ink-soft">
        <span className="font-semibold text-ink">New here?</span> Pick a project, the Demo Agent,
        and a prompt, and watch your first run record itself. It takes about a minute.
      </p>
      <Link
        to="/welcome"
        className="inline-flex h-8 items-center rounded-[var(--radius-control)] bg-accent px-3 text-sm font-medium text-accent-ink hover:brightness-110"
      >
        Start setup
      </Link>
      <IconButton
        size="sm"
        label="Dismiss setup"
        icon={<X className="size-4" />}
        onClick={() =>
          action.mutate("dismiss", {
            onError: (e) => toast.show(`Could not dismiss setup: ${describeError(e)}`, "error"),
          })
        }
      />
    </aside>
  );
}
