/**
 * Feature registry.
 *
 * Every folder in `src/features/<name>/` exports a manifest from `feature.tsx`:
 *
 *   export const feature: FeatureManifest = { id, nav?, routes };
 *
 * and may export run-detail tabs from `runDetailTabs.tsx`:
 *
 *   export const runDetailTabs: RunDetailTab[] = [...];
 *
 * and run-detail header actions from `runDetailActions.tsx`:
 *
 *   export const runDetailActions: RunDetailAction[] = [...];
 *
 * Both are collected here with `import.meta.glob`, so adding a feature never means
 * editing a central list. See docs/EXTENDING.md.
 */
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import type { RouteObject } from "react-router";

import type { RunDetail, RunEvent } from "@/api/types";

export interface NavEntry {
  label: string;
  icon: LucideIcon;
  /** Lower comes first. Core: Overview 10, Runs 20, Workflows 30, Schedules 40, Prompts 50, Agents 60, Projects 70, Approvals 80, Settings 90. */
  order: number;
  path: string;
  /** Match the path exactly (for "/"). */
  end?: boolean;
}

export interface FeatureManifest {
  id: string;
  nav?: NavEntry;
  routes: RouteObject[];
}

export interface RunDetailTabContext {
  detail: RunDetail;
  events: RunEvent[];
  eventsLoading: boolean;
  live: boolean;
}

export interface RunDetailTab {
  /** Unique id; also the `?tab=` URL value. */
  id: string;
  label: string;
  /** Lower comes first. Core: Timeline 10, Attempts 30, Inputs 40, Changes 50, Artifacts 60, Evaluations 70. */
  order: number;
  render: (ctx: RunDetailTabContext) => ReactNode;
  /** The tab's count: a number (shown as a count chip) or a node, for example a component
   * that uses hooks for a query-backed count. Nothing is shown for undefined or null, so a
   * count component renders nothing while its value is unknown. */
  count?: (detail: RunDetail) => ReactNode;
  /** Hide the tab for runs it does not apply to (e.g. workflow graph for agent runs). */
  isAvailable?: (detail: RunDetail) => boolean;
}

/** A control in the run-detail header (next to Cancel/Retry/Export), e.g. Replay or Fork. */
export interface RunDetailAction {
  id: string;
  /** Lower comes first; core controls render before all actions. */
  order: number;
  render: (ctx: { detail: RunDetail }) => ReactNode;
}

type FeatureModule = { feature?: FeatureManifest };
type TabsModule = { runDetailTabs?: RunDetailTab[] };
type ActionsModule = { runDetailActions?: RunDetailAction[] };

export function collectFeatures(modules: Record<string, FeatureModule>): FeatureManifest[] {
  const byId = new Map<string, FeatureManifest>();
  for (const [path, mod] of Object.entries(modules).sort(([a], [b]) => a.localeCompare(b))) {
    const feature = mod.feature;
    if (!feature) {
      console.warn(`[agenttape] ${path} does not export "feature"`);
      continue;
    }
    if (byId.has(feature.id))
      console.warn(`[agenttape] duplicate feature id "${feature.id}" in ${path}`);
    byId.set(feature.id, feature);
  }
  return [...byId.values()];
}

export function collectRunDetailTabs(modules: Record<string, TabsModule>): RunDetailTab[] {
  const byId = new Map<string, RunDetailTab>();
  for (const [, mod] of Object.entries(modules).sort(([a], [b]) => a.localeCompare(b))) {
    for (const tab of mod.runDetailTabs ?? []) byId.set(tab.id, tab);
  }
  return [...byId.values()].sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
}

export function collectRunDetailActions(modules: Record<string, ActionsModule>): RunDetailAction[] {
  const byId = new Map<string, RunDetailAction>();
  for (const [, mod] of Object.entries(modules).sort(([a], [b]) => a.localeCompare(b))) {
    for (const action of mod.runDetailActions ?? []) byId.set(action.id, action);
  }
  return [...byId.values()].sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
}

export const features: FeatureManifest[] = collectFeatures(
  import.meta.glob<FeatureModule>("../features/*/feature.tsx", { eager: true }),
);

export const runDetailTabs: RunDetailTab[] = collectRunDetailTabs(
  import.meta.glob<TabsModule>("../features/*/runDetailTabs.tsx", { eager: true }),
);

export const runDetailActions: RunDetailAction[] = collectRunDetailActions(
  import.meta.glob<ActionsModule>("../features/*/runDetailActions.tsx", { eager: true }),
);

export function navEntries(list: FeatureManifest[] = features): (NavEntry & { id: string })[] {
  return list
    .filter((f): f is FeatureManifest & { nav: NavEntry } => f.nav !== undefined)
    .map((f) => ({ ...f.nav, id: f.id }))
    .sort((a, b) => a.order - b.order);
}
