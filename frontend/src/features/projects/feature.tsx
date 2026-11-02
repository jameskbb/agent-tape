import { FolderGit2 } from "lucide-react";
import { Link, useNavigate } from "react-router";

import type { FeatureManifest } from "@/app/registry";
import { Page } from "@/components/layout/Page";
import { Card } from "@/components/ui";

import { ProjectCreateForm } from "./ProjectCreateForm";
import { ProjectDetailPage } from "./ProjectDetailPage";
import { ProjectsPage } from "./ProjectsPage";

function ProjectCreatePage() {
  const navigate = useNavigate();
  return (
    <Page
      title="Add a project"
      breadcrumb={
        <Link to="/projects" className="hover:text-accent">
          Projects
        </Link>
      }
      description="Point AgentTape at a repository inside an allowlisted root. The path is resolved (symlinks and .. included) before it is checked."
    >
      <Card>
        <ProjectCreateForm
          onCreated={(p) => void navigate(`/projects/${p.id}`)}
          onCancel={() => void navigate("/projects")}
        />
      </Card>
    </Page>
  );
}

export const feature: FeatureManifest = {
  id: "projects",
  nav: { label: "Projects", icon: FolderGit2, order: 70, path: "/projects" },
  routes: [
    { path: "projects", element: <ProjectsPage /> },
    { path: "projects/new", element: <ProjectCreatePage /> },
    { path: "projects/:projectId", element: <ProjectDetailPage /> },
  ],
};
