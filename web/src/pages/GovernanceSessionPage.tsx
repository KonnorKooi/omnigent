/**
 * Read-only transcript viewer (``/settings/governance/:sessionId``). Rendered
 * as the detail view of the Governance settings section.
 *
 * Lets an administrator read a session they do not own — to audit what an
 * agent did, for whom, and in which repo — WITHOUT acquiring any way to act on
 * it. That constraint is why this page exists at all instead of pointing the
 * governance table at `ChatPage`:
 *
 * - The server hands an admin `LEVEL_OWNER` on every session, so any affordance
 *   derived from permission level unlocks here. Nothing below reads one.
 * - `ChatPage`'s `BubbleView` reads `useChatStore` for session status and the
 *   active conversation id, and offers "Fork from here". On this page the store
 *   holds a different session (or none), so those are both wrong and unsafe.
 *   The bubble dispatcher below is local and store-free.
 * - `ApprovalCard` defaults its submitter to `chatStore.submitApproval`, which
 *   would POST a verdict against whatever session the store holds. No card
 *   reaches that path here: every elicitation item is intercepted below and
 *   rendered as a static note, and this version's `BlockRenderer` renders no
 *   approval cards at all, so the segments it does receive carry no verdict
 *   affordance to submit.
 *
 * `FileViewerContext` and `ForkDialogContext` are explicitly RESET to `null`
 * around the transcript. This page renders through `AppShell`'s `<Outlet />`,
 * which sits inside both providers — so they are live here, and simply "not
 * providing" them would leave `useFileViewer()` returning AppShell's real
 * `openFile`. Resetting turns file paths back into plain code spans and hides
 * the fork action, making the read-only behaviour structural rather than a
 * side effect of AppShell happening to have no `conversationId` on this route.
 */

import { type ReactNode, useMemo } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeftIcon, FileTextIcon, ImageIcon } from "lucide-react";
import { InfiniteScrollSentinel } from "@/components/InfiniteScrollSentinel";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { ForkDialogContextProvider } from "@/shell/ForkDialogContext";
import { ModelUsageBreakdown } from "@/components/AgentInfo";
import { formatSessionCostUsd } from "@/lib/formatCost";
import { Message, MessageContent } from "@/components/ai-elements/message";
import { BlockRenderer, FilePathAwareMessageResponse } from "@/components/blocks/BlockRenderer";
import { InertLinksContext } from "@/components/blocks/ChatMarkdown";
import { CompactionMarker, RoutingDecisionCard } from "@/components/blocks/StatusBlocks";
import { PageScroll } from "@/components/PageScroll";
import { SessionImage } from "@/components/SessionImage";
import { SourceBadge } from "@/components/SourceBadge";
import { useIsAdminStatus } from "@/hooks/useIsAdmin";
import { castAskUserQuestionPayload, parseAskUserQuestionPreview } from "@/lib/askUserQuestion";
import type { MessageContentBlock } from "@/lib/blocks";
import { fetchGovernanceSession } from "@/lib/governanceApi";
import { formatPreview } from "@/lib/previewFormat";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { type Bubble, buildBubbles, type RenderItem } from "@/lib/renderItems";
import { Link } from "@/lib/routing";
import { fetchSessionItemsPageAsc, getSession } from "@/lib/sessionsApi";

/** Matches both attachment markers the native executors emit. */
const ATTACHED_RE = /\[Attached(?: file)?:\s*([^\]]*)\]\s*/g;

/** Placeholder for a field the session has no value for. */
const DASH = "—";

/**
 * Render a session's spend. `null` means never priced, which is a different
 * answer from "cost nothing" — `$0.00` there asserts a zero the server never
 * computed.
 */
function costLabel(totalCostUsd: number | null | undefined): string {
  return totalCostUsd == null ? DASH : formatSessionCostUsd(totalCostUsd);
}

/** One labelled header field. Callers pass {@link DASH} for a missing value. */
function HeaderField({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <span className="flex items-baseline gap-1.5">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className={mono ? "font-mono text-xs" : undefined}>{value}</span>
    </span>
  );
}

/** Plain text of a user message, with attachment markers stripped. */
function userText(content: MessageContentBlock[]): string {
  return content
    .filter(
      (c): c is Extract<MessageContentBlock, { type: "input_text" }> => c.type === "input_text",
    )
    .map((c) => c.text)
    .join("")
    .replace(ATTACHED_RE, "")
    .trim();
}

/** Workspace paths carried in as `[Attached: …]` markers rather than blocks. */
function attachedPaths(content: MessageContentBlock[]): string[] {
  const joined = content
    .filter(
      (c): c is Extract<MessageContentBlock, { type: "input_text" }> => c.type === "input_text",
    )
    .map((c) => c.text)
    .join("");
  return [...joined.matchAll(ATTACHED_RE)].map((m) => (m[1] ?? "").trim()).filter((p) => p !== "");
}

/** A file/folder chip. Never a link — this page opens nothing. */
function AttachmentChip({ label }: { label: string }) {
  return (
    <span className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground">
      <FileTextIcon className="size-3 shrink-0" />
      <span className="max-w-[180px] truncate" title={label}>
        {label}
      </span>
    </span>
  );
}

/**
 * A user message. Local rather than `ChatPage`'s `UserBubble`, which reads the
 * chat store's `conversationId` to build attachment URLs — on this page that
 * is the wrong session or null, so the id is threaded in as a prop instead.
 */
function ReadOnlyUserBubble({
  bubble,
  sessionId,
}: {
  bubble: Extract<Bubble, { kind: "user" }>;
  sessionId: string;
}) {
  const text = userText(bubble.content);
  const images = bubble.content.filter(
    (c): c is Extract<MessageContentBlock, { type: "input_image" }> => c.type === "input_image",
  );
  const files = bubble.content.filter(
    (c): c is Extract<MessageContentBlock, { type: "input_file" }> => c.type === "input_file",
  );
  const mentioned = attachedPaths(bubble.content);

  return (
    <Message from="user" data-testid="message-bubble" data-role="user" className="max-w-3xl">
      {/* Sessions can be shared, so "who typed this" is not answered by the
          header's single owner. Named in full rather than as ChatPage's
          avatar tint — an auditor needs to read the address, not match a
          colour. */}
      {bubble.createdBy && (
        <span
          data-testid="governance-message-author"
          className="ml-auto text-xs text-muted-foreground"
        >
          {bubble.createdBy}
        </span>
      )}
      <MessageContent>
        {images.length > 0 && (
          <div className="mb-1.5 flex flex-wrap gap-2">
            {images.map((img, i) => {
              // file_id is optional on the wire; an image without one has no
              // content to fetch and nothing to name, so it is skipped.
              const fileId = img.file_id;
              if (!fileId) return null;
              return fileId.startsWith("pending:") ? (
                <span
                  key={i}
                  className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                >
                  <ImageIcon className="size-3 shrink-0" />
                  <span className="max-w-[180px] truncate">
                    {img.filename ?? fileId.replace("pending:", "")}
                  </span>
                </span>
              ) : (
                <SessionImage
                  key={i}
                  path={`/v1/sessions/${encodeURIComponent(sessionId)}/resources/files/${encodeURIComponent(fileId)}/content`}
                  alt={img.filename ?? fileId}
                  className="max-h-64 max-w-full rounded-md object-contain"
                />
              );
            })}
          </div>
        )}
        {(files.length > 0 || mentioned.length > 0) && (
          <div className="mb-1.5 flex flex-wrap gap-1.5">
            {files.map((f, i) => (
              <AttachmentChip key={`f${i}`} label={f.filename ?? f.file_id ?? "attachment"} />
            ))}
            {mentioned.map((path) => (
              <AttachmentChip key={`m${path}`} label={`@${path}`} />
            ))}
          </div>
        )}
        {text && <FilePathAwareMessageResponse breaks>{text}</FilePathAwareMessageResponse>}
      </MessageContent>
    </Message>
  );
}

/** Human label for a recorded elicitation outcome. */
function elicitationVerdict(item: Extract<RenderItem, { kind: "elicitation" }>): string {
  if (item.status === "pending") return "Pending";
  switch (item.response?.action) {
    case "accept":
      return "Approved";
    case "decline":
      return "Rejected";
    case "cancel":
      return "Cancelled";
    case "auto_resolved":
      return "Resolved elsewhere";
    default:
      return "Responded";
  }
}

/** Small label above one part of an elicitation record. */
function ElicitationLabel({ children }: { children: ReactNode }) {
  return <span className="text-xs text-muted-foreground">{children}</span>;
}

/**
 * A past approval prompt, rendered as a static record.
 *
 * The live `ApprovalCard` has no inert mode — a `pending` status always paints
 * enabled Approve/Reject buttons — so a pending prompt in someone else's
 * transcript is shown, not offered.
 *
 * "Shown" has to mean the actual request, not just that one happened: the
 * question this page exists to answer is *what* was approved. So the same
 * structured payloads `ApprovalCard` parses are rendered here read-only —
 * reusing its parsers (`castAskUserQuestionPayload`, `parseAskUserQuestionPreview`,
 * `formatPreview`) rather than growing a second interpretation of the wire
 * shape that could drift from it.
 */
function ReadOnlyElicitation({ item }: { item: Extract<RenderItem, { kind: "elicitation" }> }) {
  // Same precedence ApprovalCard uses: prefer the server-stamped structured
  // payload, fall back to parsing the (possibly truncated) preview.
  const ask =
    castAskUserQuestionPayload(item.askUserQuestion) ??
    parseAskUserQuestionPreview(item.contentPreview);
  const plan =
    item.exitPlanMode && typeof item.exitPlanMode.plan === "string" && item.exitPlanMode.plan
      ? item.exitPlanMode.plan
      : null;
  const codex = item.codexCommand ?? null;
  // With a structured payload the preview is transport JSON carrying internal
  // ids, not the request — the parsed render below replaces it.
  const preview =
    ask !== null || plan !== null || codex !== null ? "" : formatPreview(item.contentPreview);
  // What the responder actually chose, for AskUserQuestion prompts. Mirrors
  // ApprovalCard's `submittedAnswers`: a flat {question: answer} map.
  const answers =
    ask !== null && item.response?.content && Object.keys(item.response.content).length > 0
      ? item.response.content
      : null;

  return (
    <div
      data-testid="governance-elicitation"
      className="flex flex-col gap-1.5 rounded-md border border-border bg-muted/30 px-3 py-2"
    >
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Approval requested
        </span>
        <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
          {elicitationVerdict(item)}
        </span>
      </div>

      <p className="text-sm">{item.message}</p>

      {codex !== null && (
        <div data-testid="governance-elicitation-command" className="flex flex-col gap-1">
          {codex.reason && <span className="text-sm">{codex.reason}</span>}
          <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded bg-muted px-2 py-1 font-mono text-xs">
            {codex.command}
          </pre>
          {codex.cwd && (
            <span className="text-xs">
              <ElicitationLabel>cwd: </ElicitationLabel>
              <code className="rounded bg-muted px-1 py-0.5 font-mono">{codex.cwd}</code>
            </span>
          )}
          {codex.execPolicyAmendment && codex.execPolicyAmendment.length > 0 && (
            <span className="text-xs">
              <ElicitationLabel>exec policy: </ElicitationLabel>
              <code className="rounded bg-muted px-1 py-0.5 font-mono">
                {codex.execPolicyAmendment.join(" ")}
              </code>
            </span>
          )}
        </div>
      )}

      {plan !== null && (
        <div data-testid="governance-elicitation-plan" className="text-sm">
          {/* Markdown, matching how the plan was shown to whoever decided on
              it. File paths stay plain: the context reset is page-wide. */}
          <FilePathAwareMessageResponse>{plan}</FilePathAwareMessageResponse>
        </div>
      )}

      {ask !== null && (
        <ul data-testid="governance-elicitation-questions" className="flex flex-col gap-1.5">
          {ask.questions.map((question, i) => (
            <li key={question.id ?? `${i}`} className="text-sm">
              {question.header && <ElicitationLabel>{question.header}: </ElicitationLabel>}
              {question.question}
              {question.options.length > 0 && (
                <ul className="mt-0.5 flex flex-col gap-0.5 pl-4">
                  {question.options.map((option) => (
                    <li key={option.label} className="text-xs text-muted-foreground">
                      {option.label}
                      {option.description && ` — ${option.description}`}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}

      {answers !== null && (
        <ul data-testid="governance-elicitation-answers" className="flex flex-col gap-0.5 pl-3">
          {Object.entries(answers).map(([question, answer]) => (
            <li key={question} className="text-xs">
              <ElicitationLabel>{question}: </ElicitationLabel>
              {Array.isArray(answer) ? answer.join(", ") : String(answer)}
            </li>
          ))}
        </ul>
      )}

      {preview !== "" && (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-muted px-2 py-1 font-mono text-xs">
          {preview}
        </pre>
      )}

      {item.url && (
        // Deliberately NOT an anchor. The audit record should name where the
        // approval was handled without handing the reader a way to navigate
        // into a live approval page for a session that isn't theirs.
        <span data-testid="governance-elicitation-url" className="text-xs">
          <ElicitationLabel>Approval URL: </ElicitationLabel>
          <code className="break-all rounded bg-muted px-1 py-0.5 font-mono">{item.url}</code>
        </span>
      )}

      <p className="text-xs text-muted-foreground">
        {item.phase}
        {item.policyName !== "" && ` · ${item.policyName}`}
      </p>
    </div>
  );
}

type ItemSegment =
  | { kind: "blocks"; items: RenderItem[] }
  | { kind: "elicitation"; item: Extract<RenderItem, { kind: "elicitation" }> };

/**
 * Split an assistant bubble's items so elicitations peel off as static notes.
 * Consecutive non-elicitation items stay in ONE segment so `BlockRenderer`'s
 * tool-run collapsing ("See N steps") still applies — rendering item-by-item
 * would explode a long tool run into individual cards.
 */
function segmentItems(items: RenderItem[]): ItemSegment[] {
  const segments: ItemSegment[] = [];
  let run: RenderItem[] = [];
  const flush = () => {
    if (run.length > 0) segments.push({ kind: "blocks", items: run });
    run = [];
  };
  for (const item of items) {
    if (item.kind === "elicitation") {
      flush();
      segments.push({ kind: "elicitation", item });
    } else {
      run.push(item);
    }
  }
  flush();
  return segments;
}

/**
 * An assistant turn. `sessionStatus` is pinned to `"idle"`: this is committed
 * history, so nothing should render a live spinner or a streaming tail.
 */
function ReadOnlyAssistantBubble({ bubble }: { bubble: Extract<Bubble, { kind: "assistant" }> }) {
  if (bubble.items.length === 0) return null;
  const segments = segmentItems(bubble.items);
  const isWide = segments.some((s) => s.kind === "elicitation");

  return (
    <Message
      from="assistant"
      data-testid="message-bubble"
      data-role="assistant"
      className={isWide ? "max-w-full" : "max-w-3xl"}
    >
      <MessageContent className={isWide ? "w-full" : undefined}>
        {segments.map((segment, i) =>
          segment.kind === "elicitation" ? (
            <ReadOnlyElicitation key={`elic:${segment.item.elicitationId}`} item={segment.item} />
          ) : (
            <BlockRenderer key={`blocks:${i}`} items={segment.items} sessionStatus="idle" />
          ),
        )}
      </MessageContent>
      {bubble.lifecycle === "cancelled" && (
        <p className="mt-1 text-xs text-muted-foreground">Interrupted</p>
      )}
    </Message>
  );
}

/**
 * Store-free replacement for `ChatPage`'s `BubbleView`. Same dispatch, minus
 * every store read and every write affordance.
 */
function ReadOnlyBubble({ bubble, sessionId }: { bubble: Bubble; sessionId: string }) {
  if (bubble.kind === "user") return <ReadOnlyUserBubble bubble={bubble} sessionId={sessionId} />;
  // A hydrated transcript never carries the in-progress spinner variant, but
  // treat it as the completed marker rather than dropping the event.
  if (bubble.kind === "compaction" || bubble.kind === "compaction_loading") {
    return <CompactionMarker />;
  }
  if (bubble.kind === "routing_decision") {
    return (
      <RoutingDecisionCard
        model={bubble.model}
        applied={bubble.applied}
        rationale={bubble.rationale}
        agent={bubble.agent}
      />
    );
  }
  return <ReadOnlyAssistantBubble bubble={bubble} />;
}

/** Stable React key for a bubble. Mirrors `ChatPage`'s keying. */
function bubbleKey(bubble: Bubble): string {
  return bubble.kind === "assistant" ? `a:${bubble.stableId}` : `${bubble.kind}:${bubble.itemId}`;
}

export function GovernanceSessionPage({ sessionId }: { sessionId: string }) {
  const { isAdmin, isPending: isAdminPending } = useIsAdminStatus();
  // Governance-scoped keys throughout. `["session", id]` is shared by the chat
  // surface's slim (item-less) snapshot; reusing it would let a cached slim
  // entry serve this page, which needs the transcript.
  const sessionQuery = useQuery({
    queryKey: ["governance-session", sessionId],
    queryFn: () => getSession(sessionId),
    enabled: isAdmin,
    staleTime: 30_000,
  });
  // Provenance the plain snapshot doesn't carry: owner and source. Admin-gated
  // and audited server-side, so the read is itself recorded — which is why
  // this one query is never served stale: caching it would let repeat visits
  // to another member's transcript go unrecorded.
  const metaQuery = useQuery({
    queryKey: ["governance-session-meta", sessionId],
    queryFn: () => fetchGovernanceSession(sessionId),
    enabled: isAdmin,
    staleTime: 0,
  });
  const itemsQuery = useInfiniteQuery({
    queryKey: ["governance-session-items", sessionId],
    queryFn: ({ pageParam }) =>
      fetchSessionItemsPageAsc(sessionId, { newerThan: pageParam as string | undefined }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => {
      if (!lastPage.hasMore || lastPage.items.length === 0) return undefined;
      return lastPage.items[lastPage.items.length - 1].id;
    },
    enabled: isAdmin,
    staleTime: 30_000,
  });

  const items = useMemo(
    () => (itemsQuery.data?.pages ?? []).flatMap((p) => p.items),
    [itemsQuery.data],
  );
  // `null` activeResponse and no cache: this is finished history, so every
  // bubble is `completed` and there is no streaming edge to reuse across calls.
  const bubbles = useMemo(
    () => (items.length === 0 ? [] : buildBubbles(itemsToBlocks(items), null)),
    [items],
  );

  // Until `/v1/me` answers, `isAdmin` is a seeded `false` that means "not asked
  // yet" as often as "not an admin" — denying now accuses a real admin who
  // deep-linked here on a cold load.
  if (isAdminPending) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <PageScroll contentClassName="px-8" extraBottom="2.5rem">
        <h1 className="mb-2 text-2xl font-semibold">Governance</h1>
        <p className="text-sm text-muted-foreground">
          You don't have permission to view governance data.
        </p>
      </PageScroll>
    );
  }

  const session = sessionQuery.data;
  const meta = metaQuery.data;
  const title = session?.title ?? meta?.title ?? "Untitled session";
  const workspace = session?.workspace ?? meta?.workspace ?? null;
  // Both sources carry the cost; fall back so a failed snapshot doesn't read
  // as "never priced" when the governance row knows the figure.
  const totalCostUsd = session?.totalCostUsd ?? meta?.totalCostUsd ?? null;
  const usageByModel = session?.usageByModel ?? null;
  const loadError = sessionQuery.error ?? metaQuery.error ?? itemsQuery.error;

  // `fetchGovernanceSession` is the ONLY call that writes the access audit
  // row — `GET /v1/sessions/{id}/items` is not audited. So the transcript is
  // gated on that query succeeding, not merely on the items arriving: a
  // failed or 404'd governance read must not leave an admin reading another
  // member's session with nothing in the log. The three queries are
  // independent and unordered, which is exactly why the gate is on the
  // result rather than on call order.
  const auditRecorded = metaQuery.isSuccess;
  const stillLoading =
    (metaQuery.isPending || itemsQuery.isLoading) && !metaQuery.isError && !itemsQuery.isError;

  return (
    // Reset both chat contexts across the WHOLE page, not just the transcript.
    // AppShell wraps the outlet this page renders into, so both are live here;
    // scoping the reset to the transcript would leave the header one future
    // addition (a changed-files summary, a workspace link) away from silently
    // re-acquiring the live file opener with no test failing.
    <FileViewerContext.Provider value={null}>
      <ForkDialogContextProvider value={null}>
        {/* Transcript markdown is agent-authored and therefore
            attacker-authorable; an anchor here would be a navigation channel
            out of a read-only audit page, and the server hands an admin
            LEVEL_OWNER so wherever it led would work. */}
        <InertLinksContext.Provider value={true}>
          <PageScroll contentClassName="px-8" extraBottom="2.5rem" maxWidthClassName="max-w-4xl">
            <div data-testid="governance-session-header" className="mb-6">
              <Link
                to="/settings/governance"
                className="mb-3 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
              >
                <ArrowLeftIcon className="size-4" />
                Back to governance
              </Link>
              <div className="flex items-center justify-between">
                <h1 className="text-2xl font-semibold">{title}</h1>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
                {meta && <SourceBadge source={meta.source} />}
                <HeaderField label="Owner" value={meta?.owner ?? DASH} />
                <HeaderField label="Workspace" value={workspace ?? DASH} mono />
                {meta?.gitBranch && <HeaderField label="Branch" value={meta.gitBranch} />}
                {meta?.externalSessionId && (
                  <HeaderField label="Session ID" value={meta.externalSessionId} mono />
                )}
                <span data-testid="governance-session-cost" className="flex items-baseline gap-1.5">
                  <span className="text-xs uppercase tracking-wide text-muted-foreground">
                    Cost
                  </span>
                  <span className="tabular-nums">{costLabel(totalCostUsd)}</span>
                </span>
              </div>
              {usageByModel !== null && Object.keys(usageByModel).length > 0 && (
                <div className="mt-3">
                  <ModelUsageBreakdown usageByModel={usageByModel} />
                </div>
              )}
              <p className="mt-3 text-xs text-muted-foreground">
                Read-only view of another member's session. Actions are not available here.
              </p>
            </div>

            {loadError && (
              <div
                role="alert"
                className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                Could not load this session. {loadError instanceof Error ? loadError.message : ""}
              </div>
            )}

            {stillLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

            {auditRecorded && (
              <>
                {items.length > 0 && meta && (
                  <p className="mb-3 text-xs text-muted-foreground">
                    Showing {items.length} of {meta.itemCount} items
                  </p>
                )}

                {!itemsQuery.isLoading && !itemsQuery.isError && bubbles.length === 0 && (
                  <p className="text-sm text-muted-foreground">
                    This session has no transcript items.
                  </p>
                )}

                <div className="flex flex-col gap-4">
                  {bubbles.map((bubble) => (
                    <ReadOnlyBubble key={bubbleKey(bubble)} bubble={bubble} sessionId={sessionId} />
                  ))}
                </div>

                <InfiniteScrollSentinel
                  className="mt-3"
                  hasMore={itemsQuery.hasNextPage}
                  isFetching={itemsQuery.isFetchingNextPage}
                  fetchMore={itemsQuery.fetchNextPage}
                />
              </>
            )}
          </PageScroll>
        </InertLinksContext.Provider>
      </ForkDialogContextProvider>
    </FileViewerContext.Provider>
  );
}
