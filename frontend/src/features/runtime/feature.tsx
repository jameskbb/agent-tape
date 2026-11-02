import type { FeatureManifest } from "@/app/registry";

import { RuntimeSettingsPage } from "./RuntimeSettingsPage";

// Lives under Settings (no nav entry); linked from the Settings page.
export const feature: FeatureManifest = {
  id: "runtime",
  routes: [{ path: "settings/runtime", element: <RuntimeSettingsPage /> }],
};
