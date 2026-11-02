/** Registers the "Files" run-detail tab (File Evidence), just before "Changes". */
import type { RunDetailTab } from "@/app/registry";

import { FilesTab } from "./FilesTab";

export const runDetailTabs: RunDetailTab[] = [
  {
    id: "files",
    label: "Files",
    order: 45,
    render: ({ detail, live }) => <FilesTab key={detail.run.id} detail={detail} live={live} />,
  },
];
