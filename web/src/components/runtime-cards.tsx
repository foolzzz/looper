import { AlertTriangle, CheckCircle2, CircleDashed, Loader2, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useAction } from "@/lib/use-action";
import { probeRuntime, type AgentRole, type Runtime, type RuntimeType } from "@/lib/api";
import { PLANNED_RUNTIMES, RUNTIME_TYPES, roleLabel, type RuntimeKind } from "@/lib/runtime-catalog";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { useRegistry } from "@/lib/use-registry";
import { cn } from "@/lib/utils";

const ROLES: AgentRole[] = ["manager", "planner", "worker", "reviewer"];
const TTADK_RUNTIMES = [
  { type: "coco", runtime_id: "rt-ttadk-coco", name: "ttadk-coco" },
  { type: "codex", runtime_id: "rt-ttadk-codex", name: "ttadk-codex" },
  { type: "cursor-agent", runtime_id: "rt-ttadk-cursor-agent", name: "ttadk-cursor-agent" },
] as const;
type TtadkRuntime = (typeof TTADK_RUNTIMES)[number];

type Detection = "detected" | "missing" | "unchecked";

function detection(runtime: Runtime | undefined): Detection {
  if (!runtime || runtime.probe_error) return "missing";
  return runtime.version ? "detected" : "unchecked";
}

const DETECTION: Record<Detection, { label: string; variant: "success" | "warning" | "outline"; icon: typeof CheckCircle2 }> = {
  detected: { label: "Detected", variant: "success", icon: CheckCircle2 },
  missing: { label: "Not detected", variant: "warning", icon: AlertTriangle },
  unchecked: { label: "Not checked yet", variant: "outline", icon: CircleDashed },
};

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-2 text-xs">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function CardHead({ title, kind, badges }: { title: string; kind: RuntimeKind; badges: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      <span className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">{kind}</span>
      <div className="ml-auto flex flex-wrap items-center gap-1.5">{badges}</div>
    </div>
  );
}

/** One runtime: a type's built-in one (`runtime` absent until it is found) or one registered by hand before. */
function RuntimeCard({ type, runtime, launcher }: { type: RuntimeType | undefined; runtime: Runtime | undefined; launcher?: TtadkRuntime }) {
  const action = useAction();
  const info = RUNTIME_TYPES[runtime?.type ?? launcher?.type ?? type?.type ?? ""];
  const ttadk = runtime?.launcher === "ttadk" || !!launcher;
  const custom = runtime ? !runtime.builtin : false;
  const inProcess = runtime ? runtime.in_process : type?.in_process ?? false;
  const probeError = runtime?.probe_error ?? type?.probe_error;
  const state = ttadk && !runtime?.path ? "missing" : detection(runtime);
  const { label, variant, icon: Icon } = DETECTION[state];
  const capabilities = runtime ? runtime.capabilities : launcher ? [] : type?.capabilities ?? [];
  const roles = runtime ? runtime.roles : launcher ? [] : type?.roles ?? [];
  const command = runtime?.executable || (ttadk ? "ttadk" : runtime?.default_executable || type?.default_executable || "");
  const title = launcher?.name ?? (custom || ttadk ? runtime!.name : info?.title ?? type?.label ?? runtime?.type ?? "");
  const redetect = () => action.run(() => probeRuntime(runtime?.runtime_id ?? launcher?.runtime_id ?? type!.runtime_id));

  return (
    <li className={cn("space-y-3 rounded-lg border bg-card px-5 py-4", state === "detected" ? "border-border" : "border-dashed border-border")}>
      <CardHead
        title={title}
        kind={info?.kind ?? "CLI"}
        badges={
          <>
            {custom && <Badge variant="outline">custom</Badge>}
            {ttadk && <Badge variant="outline">TTADK</Badge>}
            <Badge variant={variant}>
              <Icon className="mr-1 h-3 w-3" aria-hidden="true" />
              {label}
            </Badge>
          </>
        }
      />
      <dl className="space-y-1.5">
        {custom && <Fact label="Type">{runtime!.type_label}</Fact>}
        {(!inProcess || state !== "missing") && <Fact label="Path">
          {inProcess ? "Runs inside looper serve" : runtime?.path ? (
            <span className="font-mono">{runtime.path}</span>
          ) : (
            <span className="text-muted-foreground">
              <span className="font-mono text-foreground">{command}</span> is not on the server&apos;s PATH
            </span>
          )}
        </Fact>}
        <Fact label="Version">
          {runtime?.version ? (
            <span className="font-mono">{runtime.version}</span>
          ) : !runtime?.probe_error ? (
            <span className="text-muted-foreground">{runtime ? "not checked yet: Re-detect" : "not found"}</span>
          ) : null}
          {runtime?.probe_error && !inProcess ? (
            <span className="text-warning">{runtime.version ? " · " : ""}{runtime.probe_error}</span>
          ) : null}
          {runtime?.probed_at && (
            <span className="text-muted-foreground"> · checked {new Date(runtime.probed_at).toLocaleString()}</span>
          )}
          {type?.loads_operator_config && (
            <p className="text-warning">
              Runs with your own Cursor login and HOME: your personal Cursor and Claude Code configuration (rules, hooks, MCP servers, skills, plugins) applies to these runs and cannot be turned off.
            </p>
          )}
        </Fact>
        <Fact label="Roles">
          <span className="flex flex-wrap gap-x-3 gap-y-0.5">
            {ROLES.map((role) => {
              const can = roles.includes(role);
              return (
                <span key={role} className={can ? "text-foreground" : "text-muted-foreground line-through"}>
                  {roleLabel(role)}
                  <span className="sr-only">{can ? ": can run" : ": cannot run"}</span>
                </span>
              );
            })}
          </span>
        </Fact>
        <Fact label="Capabilities">
          <span className="font-mono text-[11px] text-muted-foreground">
            {capabilities.length ? capabilities.join(", ") : "none"}
          </span>
        </Fact>
        {runtime && (
          <Fact label={AGENT_PROFILE.plural}>
            {runtime.profile_count === 0 ? "none" : `${runtime.profile_count} ${runtime.profile_count === 1 ? "uses" : "use"} it`}
          </Fact>
        )}
      </dl>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="min-w-0 flex-1 text-[11px] text-muted-foreground">
          {inProcess
            ? state === "detected"
              ? "Comes with Looper; Re-detect checks its Python package."
              : probeError ?? "Re-detect checks its Python package."
            : ttadk && !runtime?.path
            ? "Install TTADK and sign in (ttadk auth login sso), then Re-detect."
            : custom
            ? "Registered by hand before runtimes were built in; read-only, and kept while an Agent Profile uses it."
            : state === "detected"
              ? `Found as ${command} on the server's PATH.`
              : info?.install
                ? <>Install: <code className="font-mono text-foreground">{info.install}</code>, then Re-detect.</>
                : <>Put <code className="font-mono text-foreground">{command}</code> on the server&apos;s PATH, then Re-detect.</>}
        </p>
        <Button type="button" size="sm" variant="outline" onClick={redetect} disabled={action.busy} aria-label={`Re-detect ${title}`}>
          {action.busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          Re-detect
        </Button>
      </div>
      {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
    </li>
  );
}

/**
 * Settings → Agent Runtimes, read-only: built-in runtime types, ones registered by hand before, and planned types.
 */
export function RuntimesSection() {
  const { registry, error } = useRegistry();
  if (!registry) {
    return error ? (
      <div role="alert" className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">{error}</div>
    ) : (
      <div className="flex h-32 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
      </div>
    );
  }
  const builtin = (type: RuntimeType) => registry.runtimes.find((r) => r.builtin && r.runtime_id === type.runtime_id);
  const additional = registry.runtimes.filter((r) => !r.builtin || r.launcher === "ttadk");
  return (
    <ul className="grid gap-4 xl:grid-cols-2">
      {registry.runtime_types.map((type) => (
        <RuntimeCard key={type.type} type={type} runtime={builtin(type)} />
      ))}
      {additional.map((runtime) => (
        <RuntimeCard key={runtime.runtime_id} type={registry.runtime_types.find((t) => t.type === runtime.type)} runtime={runtime} />
      ))}
      {TTADK_RUNTIMES.filter((launcher) => !registry.runtimes.some((runtime) => runtime.runtime_id === launcher.runtime_id)).map((launcher) => (
        <RuntimeCard key={launcher.runtime_id} type={registry.runtime_types.find((type) => type.type === launcher.type)} runtime={undefined} launcher={launcher} />
      ))}
      {PLANNED_RUNTIMES.map((planned) => (
        <li key={planned.title} className="space-y-2 rounded-lg border border-dashed border-border px-5 py-4 opacity-80">
          <CardHead title={planned.title} kind={planned.kind} badges={<Badge variant="outline">Planned</Badge>} />
          <p className="text-xs text-muted-foreground">Not available yet: Looper cannot run it, and it cannot be selected.</p>
        </li>
      ))}
    </ul>
  );
}
