import { MessageSquareQuote } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { FindingsInboxPage } from "./FindingsInboxPage";

export const feature: FeatureManifest = {
  id: "findings",
  nav: { label: "Findings", icon: MessageSquareQuote, order: 15, path: "/findings" },
  routes: [{ path: "findings", element: <FindingsInboxPage /> }],
};
