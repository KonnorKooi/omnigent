// Settings → MCP Servers: the MCP servers registered with the harness CLIs on
// a host.
//
// Host-backed, not server-local: an MCP server is only usable on the machine
// where the harness CLI runs, so everything here is proxied to a host. Two
// consequences shape the UI:
//
//   1. Secrets are write-only. The list reports env/header KEY NAMES, never
//      values, so an existing server's secrets cannot be shown back — editing
//      one asks for its secrets again rather than pre-filling them.
//   2. Writes do not affect running sessions. A harness reads its MCP config
//      at startup, so every successful write says so.

import { useState } from "react";
import { PlayIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { showToast } from "@/components/ui/toast";
import { Spinner } from "@/components/ui/spinner";
import {
  MCP_HARNESSES,
  type McpHarness,
  type McpServerDraft,
  type McpServerInfo,
  useAddMcpServer,
  useMcpServers,
  useProbeMcpServer,
  useRemoveMcpServer,
} from "@/hooks/useMcpConfig";

/** Human labels for the resolvability status the listing reports. */
const STATUS_LABEL: Record<string, string> = {
  resolvable: "Command found",
  unresolvable: "Command missing",
  configured: "Configured",
  unknown: "Unknown",
};

function StatusBadge({ status }: { status: string }) {
  // "Command found" is deliberately not styled as success: it only means the
  // executable resolves on PATH. Only a probe proves the server works.
  const variant = status === "unresolvable" ? "destructive" : "outline";
  return (
    <Badge variant={variant} className="font-normal">
      {STATUS_LABEL[status] ?? status}
    </Badge>
  );
}

/** One row: what the server is, plus probe and remove. */
function ServerRow({ server }: { server: McpServerInfo }) {
  const probe = useProbeMcpServer();
  const remove = useRemoveMcpServer();
  const [confirming, setConfirming] = useState(false);

  const onProbe = async () => {
    try {
      const result = await probe.mutateAsync({ name: server.name, harness: server.harness });
      showToast(
        result.usable
          ? `${server.name} works — ${result.tool_count} tool${result.tool_count === 1 ? "" : "s"} listed.`
          : `${server.name} is not usable: ${result.detail}`,
      );
    } catch (err) {
      showToast(`Probe failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const onRemove = async () => {
    try {
      const result = await remove.mutateAsync({ name: server.name, harness: server.harness });
      showToast(`Removed ${server.name}. ${result.detail}`);
      setConfirming(false);
    } catch (err) {
      showToast(`Remove failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border py-3 last:border-b-0">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{server.name}</span>
          <Badge variant="secondary" className="font-normal">
            {server.harness}
          </Badge>
          <Badge variant="outline" className="font-normal">
            {server.transport}
          </Badge>
          <StatusBadge status={server.status} />
        </div>
        <div className="mt-1 truncate font-mono text-sm text-muted-foreground">
          {server.command
            ? [server.command, ...(server.args ?? [])].join(" ")
            : (server.url ?? "—")}
        </div>
        {/* Key names only — the values live on the host and are never returned. */}
        {server.env_keys?.length || server.header_keys?.length ? (
          <div className="mt-1 text-sm text-muted-foreground">
            {server.env_keys?.length ? `env: ${server.env_keys.join(", ")}` : null}
            {server.env_keys?.length && server.header_keys?.length ? " · " : null}
            {server.header_keys?.length ? `headers: ${server.header_keys.join(", ")}` : null}
          </div>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button variant="outline" size="sm" onClick={onProbe} loading={probe.isPending}>
          <PlayIcon className="mr-1.5 size-3.5" />
          Test
        </Button>
        {confirming ? (
          <>
            <Button variant="destructive" size="sm" onClick={onRemove} loading={remove.isPending}>
              Confirm
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </>
        ) : (
          <Button variant="ghost" size="sm" onClick={() => setConfirming(true)}>
            <Trash2Icon className="size-3.5" />
          </Button>
        )}
      </div>
    </div>
  );
}

/** Add-server dialog. Secrets are entered here and never read back. */
function AddServerDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const add = useAddMcpServer();
  const [harness, setHarness] = useState<McpHarness>("claude");
  const [transport, setTransport] = useState<"stdio" | "http" | "sse">("stdio");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  // One "KEY=value" per line. Values are secrets: they go up and never return.
  const [envText, setEnvText] = useState("");
  const [headerText, setHeaderText] = useState("");

  const isStdio = transport === "stdio";

  const parsePairs = (text: string): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const eq = trimmed.indexOf("=");
      if (eq <= 0) continue;
      out[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
    }
    return out;
  };

  const reset = () => {
    setName("");
    setCommand("");
    setArgs("");
    setUrl("");
    setEnvText("");
    setHeaderText("");
  };

  const onSubmit = async () => {
    const draft: McpServerDraft = isStdio
      ? {
          harness,
          name: name.trim(),
          transport,
          command: command.trim(),
          args: args.split(/\s+/).filter(Boolean),
          env: parsePairs(envText),
        }
      : {
          harness,
          name: name.trim(),
          transport,
          url: url.trim(),
          headers: parsePairs(headerText),
        };
    try {
      const result = await add.mutateAsync(draft);
      showToast(`Added ${result.name}. ${result.detail}`);
      reset();
      onClose();
    } catch (err) {
      showToast(`Add failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const canSubmit = name.trim() !== "" && (isStdio ? command.trim() !== "" : url.trim() !== "");

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add MCP server</DialogTitle>
          <DialogDescription>
            A stdio server is a command the harness executes on the host. Only add servers you
            trust.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="flex gap-3">
            <label className="flex-1 text-sm">
              Harness
              <Select value={harness} onValueChange={(v) => setHarness(v as McpHarness)}>
                <SelectTrigger className="mt-1 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MCP_HARNESSES.map((h) => (
                    <SelectItem key={h} value={h}>
                      {h}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
            <label className="flex-1 text-sm">
              Transport
              <Select
                value={transport}
                onValueChange={(v) => setTransport(v as "stdio" | "http" | "sse")}
              >
                <SelectTrigger className="mt-1 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="stdio">stdio</SelectItem>
                  <SelectItem value="http">http</SelectItem>
                  <SelectItem value="sse">sse</SelectItem>
                </SelectContent>
              </Select>
            </label>
          </div>
          <label className="block text-sm">
            Name
            <Input
              className="mt-1"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="github"
            />
          </label>
          {isStdio ? (
            <>
              <label className="block text-sm">
                Command
                <Input
                  className="mt-1"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="npx"
                />
              </label>
              <label className="block text-sm">
                Arguments
                <Input
                  className="mt-1"
                  value={args}
                  onChange={(e) => setArgs(e.target.value)}
                  placeholder="-y @acme/mcp-server"
                />
              </label>
              <label className="block text-sm">
                Environment (KEY=value per line)
                <textarea
                  className="mt-1 min-h-20 w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm"
                  value={envText}
                  onChange={(e) => setEnvText(e.target.value)}
                  placeholder="GITHUB_TOKEN=…"
                />
              </label>
            </>
          ) : (
            <>
              <label className="block text-sm">
                URL
                <Input
                  className="mt-1"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://example.com/mcp"
                />
              </label>
              <label className="block text-sm">
                Headers (Name=value per line)
                <textarea
                  className="mt-1 min-h-20 w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm"
                  value={headerText}
                  onChange={(e) => setHeaderText(e.target.value)}
                  placeholder="Authorization=Bearer …"
                />
              </label>
            </>
          )}
          <p className="text-sm text-muted-foreground">
            Secrets are sent to the host and never shown again — this screen can only report which
            keys a server uses.
          </p>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={onSubmit} disabled={!canSubmit} loading={add.isPending}>
            Add server
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Settings → MCP Servers. */
export function McpServersSection() {
  const { data, isPending, error } = useMcpServers();
  const [adding, setAdding] = useState(false);

  return (
    <section>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">MCP Servers</h1>
        <Button size="sm" onClick={() => setAdding(true)}>
          <PlusIcon className="mr-1.5 size-3.5" />
          Add server
        </Button>
      </div>
      <p className="mt-1 text-ui text-muted-foreground">
        MCP servers registered with the harness CLIs on your connected host. Changes apply to newly
        started sessions — a running harness reads its MCP config at startup.
      </p>

      <div className="mt-6">
        {isPending ? (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Spinner className="size-4" />
            Reading the host&rsquo;s config…
          </div>
        ) : error ? (
          // A host that is offline or absent is the common failure here, and
          // it reads very differently from "no servers configured".
          <div className="rounded-md border border-border p-4 text-ui text-muted-foreground">
            Could not read MCP config from the host:{" "}
            {error instanceof Error ? error.message : String(error)}
          </div>
        ) : !data || data.servers.length === 0 ? (
          <div className="rounded-md border border-border p-4 text-ui text-muted-foreground">
            No MCP servers are configured on this host yet.
          </div>
        ) : (
          <div>
            {data.servers.map((s) => (
              <ServerRow key={`${s.harness}:${s.name}`} server={s} />
            ))}
          </div>
        )}
      </div>

      <AddServerDialog open={adding} onClose={() => setAdding(false)} />
    </section>
  );
}

export default McpServersSection;
