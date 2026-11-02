import { ListVideo } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { RunDetailPage } from "./RunDetailPage";
import { RunsPage } from "./RunsPage";

export const feature: FeatureManifest = {
  id: "runs",
  nav: { label: "Runs", icon: ListVideo, order: 20, path: "/runs" },
  routes: [
    { path: "runs", element: <RunsPage /> },
    { path: "runs/:runId", element: <RunDetailPage /> },
  ],
};
