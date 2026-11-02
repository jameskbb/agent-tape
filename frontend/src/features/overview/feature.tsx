import { Gauge } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { OverviewPage } from "./OverviewPage";

export const feature: FeatureManifest = {
  id: "overview",
  nav: { label: "Overview", icon: Gauge, order: 10, path: "/", end: true },
  routes: [{ index: true, element: <OverviewPage /> }],
};
