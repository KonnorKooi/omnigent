// A sentinel row that asks for the next page when it scrolls into view.
//
// Cursor-paged lists (governance sessions, a transcript) render this after
// the last row. An IntersectionObserver fires as it approaches the viewport,
// so the next page is already loading by the time the user reaches the
// bottom.

import { useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface InfiniteScrollSentinelProps {
  /** Whether another page exists; the sentinel is inert without one. */
  hasMore: boolean;
  /** Whether a page is already in flight, so we don't ask twice. */
  isFetching: boolean;
  /** Requests the next page. */
  fetchMore: () => void;
  /** Extra classes for spacing within the list. */
  className?: string;
  /**
   * Scroll container to observe within. Omit when the list scrolls with the
   * page, where the viewport is the correct root.
   */
  scrollRoot?: Element | null;
}

/** Requests the next page when it scrolls into view. */
export function InfiniteScrollSentinel({
  hasMore,
  isFetching,
  fetchMore,
  className,
  scrollRoot,
}: InfiniteScrollSentinelProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  // Held in a ref so the observer effect does not re-subscribe on every
  // render just because the caller passed a fresh closure.
  const fetchMoreRef = useRef(fetchMore);
  fetchMoreRef.current = fetchMore;

  useEffect(() => {
    const node = ref.current;
    if (!node || !hasMore || isFetching) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) fetchMoreRef.current();
      },
      {
        root: scrollRoot ?? null,
        // Start loading before the sentinel is actually visible, so paging
        // feels continuous rather than stalling at the bottom.
        rootMargin: "200px",
      },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasMore, isFetching, scrollRoot]);

  if (!hasMore) return null;
  return (
    <div ref={ref} className={cn("flex w-full justify-center", className)}>
      <Button variant="ghost" size="sm" onClick={fetchMore} loading={isFetching}>
        Load more
      </Button>
    </div>
  );
}

export default InfiniteScrollSentinel;
