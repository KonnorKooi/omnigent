// Provenance badge for a session row: did this server run the session, or was
// the transcript imported from a local harness?
//
// The distinction matters on the governance surface, where an admin is
// auditing what ran where. An imported transcript is a record of work done
// outside this server, so it carries different weight as evidence than a live
// session the server itself dispatched.

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** Wire value for a session this server ran itself. */
const SOURCE_LIVE = "live";
const IMPORT_PREFIX = "import:";

export interface SourceBadgeProps {
  /** Provenance from the governance API: `live` or `import:<source>`. */
  source: string;
  className?: string;
}

/** Renders a session's provenance as a small badge. */
export function SourceBadge({ source, className }: SourceBadgeProps) {
  // Lower-case and compact by design: this sits in a dense table row, and the
  // raw source name is what an auditor matches against the import that
  // produced it.
  const label = source.startsWith(IMPORT_PREFIX)
    ? `${source.slice(IMPORT_PREFIX.length)} import`
    : SOURCE_LIVE;
  return (
    <Badge
      data-testid="source-badge"
      variant={source === SOURCE_LIVE ? "secondary" : "outline"}
      className={cn("font-normal", className)}
    >
      {label}
    </Badge>
  );
}

export default SourceBadge;
