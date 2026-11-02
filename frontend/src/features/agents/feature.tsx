import { Bot } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { AgentsPage } from "./AgentsPage";
import { ProfileCreatePage } from "./ProfileCreatePage";
import { ProfileDetailPage } from "./ProfileDetailPage";

export const feature: FeatureManifest = {
  id: "agents",
  nav: { label: "Agents", icon: Bot, order: 60, path: "/agents" },
  routes: [
    { path: "agents", element: <AgentsPage /> },
    { path: "agents/new", element: <ProfileCreatePage /> },
    { path: "agents/:profileId", element: <ProfileDetailPage /> },
  ],
};
