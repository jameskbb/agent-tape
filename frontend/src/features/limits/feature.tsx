import type { FeatureManifest } from "@/app/registry";

import { LimitsPage } from "./LimitsPage";

// Lives under Settings (no nav entry); linked from the Settings page.
export const feature: FeatureManifest = {
  id: "limits",
  routes: [{ path: "settings/limits", element: <LimitsPage /> }],
};
