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
function RuntimeCard({ type, runtime }: { type: RuntimeType | undefined; runtime: Runtime | undefined }) {
  const action = useAction();
  const info = RUNTIME_TYPES[runtime?.type ?? type?.type ?? ""];
  const legacy = runtime ? !runtime.builtin : false;
  const state = detection(runtime);
  const { label, variant, icon: Icon } = DETECTION[state];
  const capabilities = runtime ? runtime.capabilities : type?.capabilities ?? [];
  const roles = runtime ? runtime.roles : type?.roles ?? [];
  const command = runtime?.executable || runtime?.default_executable || type?.default_executable || "";
  const title = legacy ? runtime!.name : info?.title ?? type?.label ?? runtime?.type ?? "";
  const redetect = () => action.run(() => probeRuntime(runtime?.runtime_id ?? type!.runtime_id));

  return (
    <li className={cn("space-y-3 rounded-lg border bg-card px-5 py-4", state === "detected" ? "border-border" : "border-dashed border-border")}>
      <CardHead
        title={title}
        kind={info?.kind ?? "CLI"}
        badges={
          <>
            {legacy && <Badge variant="outline">custom (legacy)</Badge>}
            <Badge variant={variant}>
              <Icon className="mr-1 h-3 w-3" aria-hidden="true" />
              {label}
            </Badge>
          </>
        }
      />
      <dl className="space-y-1.5">
        {legacy && <Fact label="Type">{runtime!.type_label}</Fact>}
        <Fact label="Path">
          {runtime?.path ? (
            <span className="font-mono">{runtime.path}</span>
          ) : (
            <span className="text-muted-foreground">
              <span className="font-mono text-foreground">{command}</span> is not on the server&apos;s PATH
            </span>
          )}
        </Fact>
        <Fact label="Version">
          {runtime?.version ? (
            <span className="font-mono">{runtime.version}</span>
          ) : runtime?.probe_error ? (
            <span className="text-warning">{runtime.probe_error}</span>
          ) : (
            <span className="text-muted-foreground">{runtime ? "not checked yet: Re-detect" : "not found"}</span>
          )}
          {runtime?.probed_at && (
            <span className="text-muted-foreground"> · checked {new Date(runtime.probed_at).toLocaleString()}</span>
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
            {runtime.agent_count === 0 ? "none" : `${runtime.agent_count} ${runtime.agent_count === 1 ? "uses" : "use"} it`}
          </Fact>
        )}
      </dl>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="min-w-0 flex-1 text-[11px] text-muted-foreground">
          {legacy
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
 * Settings → Agent Runtimes, read-only: one card per runtime type this version runs (found on the server's PATH
 * or not), the ones registered by hand before, and the planned types.
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
  const legacy = registry.runtimes.filter((r) => !r.builtin);
  return (
    <ul className="grid gap-4 xl:grid-cols-2">
      {registry.runtime_types.map((type) => (
        <RuntimeCard key={type.type} type={type} runtime={builtin(type)} />
      ))}
      {legacy.map((runtime) => (
        <RuntimeCard key={runtime.runtime_id} type={registry.runtime_types.find((t) => t.type === runtime.type)} runtime={runtime} />
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
