/** Registers the "Findings" run-detail tab, after Timeline and before Workflow. */
import type { RunDetailTab } from "@/app/registry";

import { recordsFindings } from "./capability";
import { FindingsTab, FindingsTabCount } from "./FindingsTab";

export const runDetailTabs: RunDetailTab[] = [
  {
    id: "findings",
    label: "Findings",
    order: 12,
    render: ({ detail, events, live }) => (
      <FindingsTab key={detail.run.id} detail={detail} events={events} live={live} />
    ),
    count: (detail) => <FindingsTabCount key={detail.run.id} detail={detail} />,
    isAvailable: (detail) => recordsFindings(detail.run),
  },
];
