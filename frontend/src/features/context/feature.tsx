import { BookOpenCheck } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { ContextActivityPage } from "./ContextActivityPage";

export const feature: FeatureManifest = {
  id: "context",
  nav: { label: "Context", icon: BookOpenCheck, order: 76, path: "/context" },
  routes: [{ path: "context", element: <ContextActivityPage /> }],
};
