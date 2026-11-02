import type { FeatureManifest } from "@/app/registry";

import { ComparePage } from "./ComparePage";
import { RetentionPage } from "./RetentionPage";

/** Lineage routes without their own nav entry: reached from runs and settings. */
export const feature: FeatureManifest = {
  id: "lineage",
  routes: [
    { path: "runs/compare", element: <ComparePage /> },
    { path: "settings/retention", element: <RetentionPage /> },
  ],
};
