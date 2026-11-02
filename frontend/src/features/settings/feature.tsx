import { Settings as SettingsIcon } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { SettingsPage } from "./SettingsPage";

export const feature: FeatureManifest = {
  id: "settings",
  nav: { label: "Settings", icon: SettingsIcon, order: 90, path: "/settings" },
  routes: [{ path: "settings", element: <SettingsPage /> }],
};
