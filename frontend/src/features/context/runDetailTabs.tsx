import type { RunDetailTab } from "@/app/registry";
import { recordsFindings } from "@/features/findings/capability";

import { ContextTab } from "./ContextTab";

export const runDetailTabs: RunDetailTab[] = [
  {
    id: "context",
    label: "Context",
    order: 47,
    render: ({ detail }) => <ContextTab detail={detail} />,
    isAvailable: (detail) => recordsFindings(detail.run),
  },
];
