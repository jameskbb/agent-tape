import { ShieldCheck } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { ApprovalsPage } from "./ApprovalsPage";

export const feature: FeatureManifest = {
  id: "approvals",
  nav: { label: "Approvals", icon: ShieldCheck, order: 80, path: "/approvals" },
  routes: [{ path: "approvals", element: <ApprovalsPage /> }],
};
