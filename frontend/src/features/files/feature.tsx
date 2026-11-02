import { FolderSearch } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { FileActivityIndexPage, FileActivityPage } from "./FileActivityPage";

export const feature: FeatureManifest = {
  id: "files",
  nav: { label: "File Activity", icon: FolderSearch, order: 75, path: "/files" },
  routes: [
    { path: "files", element: <FileActivityIndexPage /> },
    { path: "files/:projectId", element: <FileActivityPage /> },
  ],
};
