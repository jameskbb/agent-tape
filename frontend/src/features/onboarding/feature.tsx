import type { FeatureManifest } from "@/app/registry";

import { OnboardingPage } from "./OnboardingPage";

/** First-run setup (Journey 1). No nav entry: reached from the Overview banner or Settings. */
export const feature: FeatureManifest = {
  id: "onboarding",
  routes: [{ path: "welcome", element: <OnboardingPage /> }],
};
