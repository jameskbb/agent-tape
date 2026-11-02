import { Link } from "react-router";

import { EmptyState } from "@/components/ui";
import { Page } from "@/components/layout/Page";

export function NotFound() {
  return (
    <Page title="Page not found">
      <EmptyState
        title="There is nothing at this address"
        description="The link may be out of date, or the item was deleted."
        action={
          <Link to="/runs" className="text-accent underline-offset-2 hover:underline">
            Go to runs
          </Link>
        }
      />
    </Page>
  );
}
