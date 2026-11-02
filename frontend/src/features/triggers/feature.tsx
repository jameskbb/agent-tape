import type { FeatureManifest } from "@/app/registry";

import { TriggerDetailPage } from "./TriggerDetailPage";
import { TriggersPage } from "./TriggersPage";

// Lives under Settings (no nav entry); linked from Settings and Schedules.
export const feature: FeatureManifest = {
  id: "triggers",
  routes: [
    { path: "settings/triggers", element: <TriggersPage /> },
    { path: "settings/triggers/:triggerId", element: <TriggerDetailPage /> },
  ],
};
