import { ScrollText } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { PromptCreatePage } from "./PromptCreatePage";
import { PromptDetailPage } from "./PromptDetailPage";
import { PromptsPage } from "./PromptsPage";

export const feature: FeatureManifest = {
  id: "prompts",
  nav: { label: "Prompts", icon: ScrollText, order: 50, path: "/prompts" },
  routes: [
    { path: "prompts", element: <PromptsPage /> },
    { path: "prompts/new", element: <PromptCreatePage /> },
    { path: "prompts/:promptId", element: <PromptDetailPage /> },
  ],
};
