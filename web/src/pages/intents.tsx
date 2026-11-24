import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { LeadSources } from "@/components/lead-sources";
import { TabStrip } from "@/components/tab-strip";
import { LABEL, SELECT } from "@/components/registry-ui";
import { fetchProjectsList, fetchWorkflows, type Project, type Workflow } from "@/lib/api";
import {
  intentAlertSendersDelete, intentAlertSendersList, intentAlertSendersPut,
  intentBindingsDelete, intentBindingsList, intentBindingsPut,
  intentTypesArchive, intentTypesCreate, intentTypesHistory, intentTypesList, intentTypesRollback,
  intentTypesSave, intentTypesSetProjectTemplate,
  intentGroupReadsDelete, intentGroupReadsList, intentGroupReadsPreview, intentGroupReadsPut,
  intentGroupReadsSettingsPut, leadsChats,
  intentRecognitionsList, intentRecognitionsPreview, intentRecognitionsStart, intentRecognitionsSettingsPut,
  type IntentRecognitions, type IntentRecognitionPreview,
  type FollowedGroups, type FollowedGroupPreview, type LeadChat,
  intentsBuild, intentsList, intentsReject, intentsSettingsGet, intentsStats, intentsUpdate,
  type AlertSender, type Intent, type IntentBinding, type IntentStatsType, type IntentType, type IntentTypeVersion,
} from "@/lib/api-leads";
import {
  CONFIDENCE_ZH, EDITED_VIA_ZH, INTENT_STATE_ZH, PRIORITY_ZH, PUSH_ZH, label, rate, relative,
} from "@/lib/leads-labels";
import { errorText } from "@/lib/use-action";
import { V1Error } from "@/lib/v1";
import { cn } from "@/lib/utils";

const TABS = [
  { id: "intents", label: "意图" },
  { id: "types", label: "类型" },
  { id: "bindings", label: "会话绑定" },
  { id: "alerts", label: "告警机器人" },
  { id: "groups", label: "群需求" },
] as const;
type TabId = (typeof TABS)[number]["id"];
const INTENT_STATES = ["pending", "tasked", "rejected", "expired", "merged"];
const TEXTAREA = "w-full rounded-md border border-input bg-transparent p-3 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

function conflictOf(err: unknown): string {
  return err instanceof V1Error && err.code === "conflict" ? err.message : "";
}

/** The Intents page: pending intents, their types, chat bindings, alert bots and followed groups. */
export function IntentsPage() {
  const [tab, setTab] = useState<TabId>("intents");
  const [enabled, setEnabled] = useState<boolean | null>(null);
  useEffect(() => {
    let live = true;
    intentsSettingsGet().then(
      (settings) => { if (live) setEnabled(settings.enabled); },
      () => { if (live) setEnabled(null); },
    );
    return () => { live = false; };
  }, []);
  return (
    <div className="mx-auto max-w-5xl space-y-5 p-6 [--gutter:--spacing(6)]">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold">意图</h1>
        <p className="text-sm text-muted-foreground">从消息里认出的、还没变成任务的事。</p>
      </div>
      {enabled === false && <p role="status" className="text-sm">意图识别已关闭。建任务和驳回要等它打开；类型和绑定可以先准备。</p>}
      <TabStrip label="意图" selected={tab} className="gap-1 border-b border-border">
        {TABS.map((item, index) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`intents-tab-${item.id}`}
            aria-selected={tab === item.id}
            aria-controls={`intents-panel-${item.id}`}
            tabIndex={tab === item.id ? 0 : -1}
            onClick={() => setTab(item.id)}
            onKeyDown={(event) => {
              if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
              const next = TABS[(index + (event.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length].id;
              setTab(next);
              document.getElementById(`intents-tab-${next}`)?.focus();
            }}
            className={cn(
              "-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium pointer-coarse:min-h-11 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              tab === item.id ? "border-primary text-foreground" : "border-transparent text-muted-foreground",
            )}
          >{item.label}</button>
        ))}
      </TabStrip>
      <div role="tabpanel" id={`intents-panel-${tab}`} aria-labelledby={`intents-tab-${tab}`}>
        {tab === "intents" && <IntentsTab enabled={enabled !== false} />}
        {tab === "types" && <TypesTab />}
        {tab === "bindings" && <BindingsTab />}
        {tab === "alerts" && <AlertsTab />}
        {tab === "groups" && <GroupsTab intentsEnabled={enabled === true} />}
      </div>
    </div>
  );
}

function Notice({ loading, error, conflict, empty, onRetry }: {
  loading: boolean; error: string; conflict: string; empty: string; onRetry: () => void;
}) {
  if (loading) return <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />加载中…</p>;
  if (conflict) return <p role="status" data-state="conflict" className="text-sm">{conflict}</p>;
  if (error) return <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">{error}<Button type="button" variant="outline" size="sm" onClick={onRetry}>重试</Button></p>;
  if (empty) return <p role="status" className="text-sm text-muted-foreground">{empty}</p>;
  return null;
}

function IntentsTab({ enabled }: { enabled: boolean }) {
  const [states, setStates] = useState<string[]>(["pending"]);
  const [type, setType] = useState("");
  const [project, setProject] = useState("");
  const [chat, setChat] = useState("");
  const [intents, setIntents] = useState<Intent[] | null>(null);
  const [stats, setStats] = useState<IntentStatsType[] | null>(null);
  const [types, setTypes] = useState<IntentType[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setLoading(true);
    const args = {
      state: states,
      ...(type ? { type } : {}),
      ...(project ? { project_id: project } : {}),
      ...(chat.trim() ? { chat: chat.trim() } : {}),
    };
    Promise.all([intentsList(args), intentsStats(), intentTypesList(), fetchProjectsList(), fetchWorkflows()]).then(
      ([list, statsPage, typePage, projectPage, workflowPage]) => {
        if (!live) return;
        setIntents(list.intents);
        setStats(statsPage.types);
        setTypes(typePage.types);
        setProjects(projectPage.projects);
        setWorkflows(workflowPage);
        setError("");
        setConflict("");
      },
      (err: unknown) => {
        if (!live) return;
        setIntents(null);
        const conflictMessage = conflictOf(err);
        setConflict(conflictMessage);
        setError(conflictMessage ? "" : errorText(err));
      },
    ).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [states, type, project, chat, attempt]);

  const replace = (next: Intent) => setIntents((rows) => rows?.map((row) => row.id === next.id ? next : row) ?? rows);
  const onlyPending = states.length === 1 && states[0] === "pending" && !type && !project && !chat.trim();
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <fieldset className="space-y-1">
          <legend className={LABEL}>状态</legend>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {INTENT_STATES.map((item) => (
              <label key={item} className="flex min-h-11 items-center gap-2 text-sm sm:min-h-0">
                <input type="checkbox" className="h-4 w-4 accent-primary" checked={states.includes(item)} onChange={() => {
                  setStates((current) => current.includes(item) ? current.filter((one) => one !== item) : [...current, item]);
                }} />{label(INTENT_STATE_ZH, item)}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1 text-sm"><span className={LABEL}>类型</span>
            <select aria-label="类型" value={type} onChange={(e) => setType(e.target.value)} className={SELECT}>
              <option value="">全部类型</option>
              {types.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
            </select>
          </label>
          <label className="space-y-1 text-sm"><span className={LABEL}>项目</span>
            <select aria-label="项目" value={project} onChange={(e) => setProject(e.target.value)} className={SELECT}>
              <option value="">全部项目</option>
              {projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.name}</option>)}
            </select>
          </label>
          <label className="space-y-1 text-sm sm:col-span-2"><span className={LABEL}>会话</span>
            <Input aria-label="会话" value={chat} onChange={(e) => setChat(e.target.value)} placeholder="会话名或 id" />
          </label>
        </div>
      </div>
      {stats && <StatsStrip stats={stats} />}
      <Notice loading={loading} error={error} conflict={conflict} onRetry={() => setAttempt((n) => n + 1)}
        empty={intents && intents.length === 0 ? (onlyPending ? "待处理的意图没有了" : "没有符合条件的意图") : ""} />
      <ul className="space-y-3">
        {intents?.map((intent) => (
          <IntentRow key={intent.id} intent={intent} projects={projects} workflows={workflows} enabled={enabled}
            onChange={replace} onError={setError} />
        ))}
      </ul>
    </div>
  );
}

function StatsStrip({ stats }: { stats: IntentStatsType[] }) {
  if (stats.length === 0) return null;
  return (
    <ul aria-label="统计" className="flex flex-wrap gap-2">
      {stats.map((item) => (
        <li key={item.key} className="min-w-0 rounded-md border border-border px-3 py-2 text-xs">
          <span className="font-medium">{item.name}</span>
          <span className="text-muted-foreground"> 识别 {item.recognized}</span>
          <span className="text-muted-foreground"> 采纳率 {rate(item.adoption_rate)}</span>
          <span className="text-muted-foreground"> 驳回率 {rate(item.rejection_rate)}</span>
        </li>
      ))}
    </ul>
  );
}

function projectChoices(intent: Intent, projects: Project[]): { id: string; name: string }[] {
  const seen = new Set<string>();
  const out: { id: string; name: string }[] = [];
  const add = (id: string, name: string) => { if (id && !seen.has(id)) { seen.add(id); out.push({ id, name }); } };
  for (const candidate of intent.candidates) add(candidate.id, candidate.name);
  if (intent.project) add(intent.project.id, intent.project.name);
  for (const item of projects) add(item.project_id, item.name);
  return out;
}

function IntentRow({
  intent, projects, workflows, enabled, onChange, onError,
}: {
  intent: Intent; projects: Project[]; workflows: Workflow[]; enabled: boolean;
  onChange: (intent: Intent) => void; onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [building, setBuilding] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [taskId, setTaskId] = useState(intent.task?.id ?? "");
  const open = intent.state === "pending" || intent.state === "expired";
  const run = async (action: () => Promise<Intent | void>) => {
    setBusy(true);
    onError("");
    try {
      const next = await action();
      if (next) onChange(next);
    } catch (err) {
      onError(conflictOf(err) || errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const projectName = intent.project?.name ?? "未选项目";
  const workflowName = intent.effective_workflow?.name ?? intent.workflow?.name ?? "项目默认";
  return (
    <li className="space-y-2 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-mono text-xs">{intent.id}</span>
        <Badge>{intent.type.name}</Badge>
        <Badge variant={intent.state === "rejected" ? "destructive" : intent.state === "pending" ? "warning" : "outline"}>
          {label(INTENT_STATE_ZH, intent.state)}
        </Badge>
        <span>{label(CONFIDENCE_ZH, intent.confidence)}</span>
        {intent.flagged && <span aria-label="flagged">⚠️ 需核对</span>}
        <span className="text-muted-foreground">{label(PUSH_ZH, intent.push_state)}</span>
        <span className="text-muted-foreground">到期 {relative(intent.expires_at)}</span>
      </div>
      <p className="text-sm font-medium">{intent.title}</p>
      <p className="whitespace-pre-wrap text-sm text-muted-foreground">{intent.summary}</p>
      <LeadSources sources={intent.sources} total={intent.sources_total} />
      {intent.task && <a className="text-sm text-primary underline-offset-2 hover:underline" href={`#tasks/${intent.task.id}`}>{intent.task.title || intent.task.id}</a>}
      {taskId && !intent.task && <a className="text-sm text-primary underline-offset-2 hover:underline" href={`#tasks/${taskId}`}>任务 {taskId}</a>}
      {intent.merged_into && <p className="text-sm">已并入 <a className="text-primary underline-offset-2 hover:underline" href="#intents">{intent.merged_into}</a></p>}
      {intent.reject_reason && <p className="text-sm text-muted-foreground">驳回原因：{intent.reject_reason}</p>}
      {open && (
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1 text-sm"><span className={LABEL}>项目</span>
            <select aria-label={`${intent.id} 项目`} value={intent.project?.id ?? ""} disabled={busy} className={SELECT}
              onChange={(e) => void run(async () => (await intentsUpdate({ intent_id: intent.id, project_id: e.target.value || null })).intent)}>
              {!intent.project && <option value="">选择项目</option>}
              {projectChoices(intent, projects).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>
          <label className="space-y-1 text-sm"><span className={LABEL}>工作流</span>
            <select aria-label={`${intent.id} 工作流`} value={intent.workflow?.key ?? ""} disabled={busy} className={SELECT}
              onChange={(e) => void run(async () => (await intentsUpdate({ intent_id: intent.id, workflow_key: e.target.value || null })).intent)}>
              <option value="">项目默认</option>
              {workflows.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
            </select>
          </label>
        </div>
      )}
      {open && (
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" disabled={busy || !enabled || !intent.project} onClick={() => setBuilding(true)}>建任务</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy || !enabled} onClick={() => { setReason(""); setRejecting(true); }}>驳回</Button>
        </div>
      )}
      {building && (
        <ConfirmDialog title={`建任务 ${intent.id}`} onClose={() => setBuilding(false)}>
          <p className="text-sm">项目：{projectName}</p>
          <p className="text-sm">工作流：{workflowName}</p>
          <pre className="whitespace-pre-wrap font-sans text-sm text-muted-foreground">{intent.summary.split("\n").slice(0, 4).join("\n")}</pre>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setBuilding(false)}>取消</Button>
            <Button type="button" disabled={busy} onClick={() => void run(async () => {
              const built = await intentsBuild({
                intent_id: intent.id,
                ...(intent.project ? { project_id: intent.project.id } : {}),
                ...(intent.workflow ? { workflow_key: intent.workflow.key } : {}),
              });
              setTaskId(built.task_id);
              setBuilding(false);
              return built.intent;
            })}>建任务</Button>
          </div>
        </ConfirmDialog>
      )}
      {rejecting && (
        <ConfirmDialog title={`驳回 ${intent.id}`} onClose={() => setRejecting(false)}>
          <label htmlFor={`reject-${intent.id}`} className="text-xs text-muted-foreground">原因（可选）</label>
          <textarea id={`reject-${intent.id}`} value={reason} maxLength={200} rows={3} onChange={(e) => setReason(e.target.value)} className={TEXTAREA} />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setRejecting(false)}>取消</Button>
            <Button type="button" disabled={busy} onClick={() => void run(async () => {
              const rejected = await intentsReject({ intent_id: intent.id, ...(reason.trim() ? { reason: reason.trim() } : {}) });
              setRejecting(false);
              return rejected.intent;
            })}>驳回</Button>
          </div>
        </ConfirmDialog>
      )}
    </li>
  );
}

function TypesTab() {
  const [types, setTypes] = useState<IntentType[] | null>(null);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    Promise.all([intentTypesList(), fetchWorkflows(), fetchProjectsList()]).then(([page, workflowPage, projectPage]) => {
      if (!live) return;
      setTypes(page.types);
      setWorkflows(workflowPage);
      setProjects(projectPage.projects);
      setError("");
      setConflict("");
    }, (err: unknown) => {
      if (!live) return;
      const message = conflictOf(err);
      setConflict(message);
      setError(message ? "" : errorText(err));
    }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [attempt]);
  const current = types?.find((item) => item.key === selected) ?? null;
  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button type="button" size="sm" variant="outline" onClick={() => setCreating((value) => !value)}>新建类型</Button>
      </div>
      {creating && <TypeCreate workflows={workflows} onCreated={() => { setCreating(false); setAttempt((n) => n + 1); }} onError={setError} />}
      <Notice loading={loading} error={error} conflict={conflict} onRetry={() => setAttempt((n) => n + 1)}
        empty={types && types.length === 0 ? "还没有类型" : ""} />
      <ul className="space-y-2">
        {types?.map((item) => (
          <li key={item.key}>
            <button type="button" onClick={() => setSelected(item.key)}
              className={cn("flex w-full flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-left text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                selected === item.key ? "border-primary" : "border-border")}>
              <span className="font-medium">{item.name}</span>
              <span className="font-mono text-xs text-muted-foreground">{item.key}</span>
              {item.builtin && <Badge variant="outline">内置</Badge>}
              <span>{label(PRIORITY_ZH, item.priority)}</span>
              <span className="text-muted-foreground">{item.default_workflow ?? "无默认工作流"}</span>
              <span>{item.enabled ? "已启用" : "已关闭"}</span>
              <Badge variant={item.recommended ? "success" : "outline"}>{item.recommended ? "推荐" : "已修改"}</Badge>
            </button>
          </li>
        ))}
      </ul>
      {current && (
        <TypeEditor type={current} workflows={workflows} projects={projects}
          onChange={() => setAttempt((n) => n + 1)} onError={setError} />
      )}
    </div>
  );
}

function TypeCreate({ workflows, onCreated, onError }: { workflows: Workflow[]; onCreated: () => void; onError: (message: string) => void }) {
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [guidance, setGuidance] = useState("");
  const [priority, setPriority] = useState("normal");
  const [workflow, setWorkflow] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form className="grid gap-3 rounded-lg border border-border p-4 sm:grid-cols-2" onSubmit={(event) => {
      event.preventDefault();
      setBusy(true);
      intentTypesCreate({ key: key.trim(), name: name.trim(), guidance: guidance.trim(), priority, ...(workflow ? { default_workflow: workflow } : {}) })
        .then(() => onCreated(), (err: unknown) => onError(conflictOf(err) || errorText(err)))
        .finally(() => setBusy(false));
    }}>
      <label className="space-y-1 text-sm"><span className={LABEL}>key</span><Input aria-label="新类型 key" value={key} onChange={(e) => setKey(e.target.value)} /></label>
      <label className="space-y-1 text-sm"><span className={LABEL}>名称</span><Input aria-label="新类型名称" value={name} onChange={(e) => setName(e.target.value)} maxLength={30} /></label>
      <label className="space-y-1 text-sm sm:col-span-2"><span className={LABEL}>判断说明</span>
        <textarea aria-label="新类型判断说明" value={guidance} maxLength={2000} rows={4} onChange={(e) => setGuidance(e.target.value)} className={TEXTAREA} />
      </label>
      <label className="space-y-1 text-sm"><span className={LABEL}>优先级</span>
        <select aria-label="新类型优先级" value={priority} onChange={(e) => setPriority(e.target.value)} className={SELECT}>
          {Object.entries(PRIORITY_ZH).map(([value, text]) => <option key={value} value={value}>{text}</option>)}
        </select>
      </label>
      <label className="space-y-1 text-sm"><span className={LABEL}>默认工作流</span>
        <select aria-label="新类型工作流" value={workflow} onChange={(e) => setWorkflow(e.target.value)} className={SELECT}>
          <option value="">无</option>
          {workflows.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
        </select>
      </label>
      <div className="sm:col-span-2"><Button type="submit" size="sm" disabled={busy || !key.trim() || !name.trim() || !guidance.trim()}>创建</Button></div>
    </form>
  );
}

function TypeEditor({
  type, workflows, projects, onChange, onError,
}: {
  type: IntentType; workflows: Workflow[]; projects: Project[]; onChange: () => void; onError: (message: string) => void;
}) {
  const [name, setName] = useState(type.name);
  const [guidance, setGuidance] = useState(type.guidance);
  const [priority, setPriority] = useState(type.priority);
  const [workflow, setWorkflow] = useState(type.default_workflow ?? "");
  const [versions, setVersions] = useState<IntentTypeVersion[] | null>(null);
  const [rollback, setRollback] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [historyError, setHistoryError] = useState("");
  useEffect(() => {
    setName(type.name); setGuidance(type.guidance); setPriority(type.priority); setWorkflow(type.default_workflow ?? "");
  }, [type]);
  useEffect(() => {
    let live = true;
    intentTypesHistory({ key: type.key }).then(
      (page) => { if (live) { setVersions(page.versions); setHistoryError(""); } },
      (err: unknown) => { if (live) setHistoryError(conflictOf(err) || errorText(err)); },
    );
    return () => { live = false; };
  }, [type.key, type.version]);
  const save = (action: () => Promise<unknown>) => {
    setBusy(true);
    onError("");
    action().then(onChange, (err: unknown) => onError(conflictOf(err) || errorText(err))).finally(() => setBusy(false));
  };
  return (
    <form className="space-y-4 rounded-lg border border-border p-4" onSubmit={(event) => {
      event.preventDefault();
      save(() => intentTypesSave({ key: type.key, name: name.trim(), guidance: guidance.trim(), priority, default_workflow: workflow || null }));
    }}>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-sm"><span className={LABEL}>名称</span>
          <Input aria-label="类型名称" value={name} maxLength={30} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="space-y-1 text-sm"><span className={LABEL}>优先级</span>
          <select aria-label="优先级" value={priority} onChange={(e) => setPriority(e.target.value as IntentType["priority"])} className={SELECT}>
            {Object.entries(PRIORITY_ZH).map(([value, text]) => <option key={value} value={value}>{text}</option>)}
          </select>
        </label>
      </div>
      <label className="block space-y-1 text-sm">
        <span className="flex items-baseline justify-between gap-2"><span className={LABEL}>判断说明</span>
          <span className={cn("text-[11px] tabular-nums", guidance.length > 2000 ? "text-destructive" : "text-muted-foreground")}>{guidance.length} / 2000</span>
        </span>
        <textarea aria-label="判断说明" value={guidance} maxLength={2000} rows={5} onChange={(e) => setGuidance(e.target.value)} className={TEXTAREA} />
        <span className="text-xs text-muted-foreground">判断说明是模型看得到的内容；改动会影响识别</span>
      </label>
      <label className="block space-y-1 text-sm"><span className={LABEL}>默认工作流</span>
        <select aria-label="默认工作流" value={workflow} onChange={(e) => setWorkflow(e.target.value)} className={SELECT}>
          <option value="">无</option>
          {workflows.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
        </select>
      </label>
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm">启用</span>
        <Switch checked={type.enabled} disabled={busy} label={`启用 ${type.name}`} onChange={(next) => save(() => intentTypesSave({ key: type.key, enabled: next }))} />
      </div>
      <ProjectTemplates type={type} projects={projects} workflows={workflows} busy={busy} onSave={(projectId, workflowKey) => {
        save(() => intentTypesSetProjectTemplate({ key: type.key, project_id: projectId, workflow_key: workflowKey }));
      }} />
      <div className="flex flex-wrap justify-end gap-2">
        {!type.builtin && <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => save(() => intentTypesArchive({ key: type.key }))}>删除</Button>}
        <Button type="submit" size="sm" disabled={busy || !name.trim() || !guidance.trim()}>保存</Button>
      </div>
      <section aria-label="历史" className="space-y-2 border-t border-border pt-3">
        <h3 className="text-sm font-medium">历史</h3>
        {historyError && <p role="alert" className="text-sm text-destructive">{historyError}</p>}
        {!versions && !historyError && <p role="status" className="text-sm text-muted-foreground">加载中…</p>}
        <ul className="space-y-2">
          {versions?.map((version) => (
            <li key={version.version} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span>第 {version.version} 版 · {version.created_at.slice(0, 16).replace("T", " ")} · {label(EDITED_VIA_ZH, version.edited_via)}
                {version.rolled_back_from != null ? ` · 从第 ${version.rolled_back_from} 版回退` : ""}</span>
              <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setRollback(version.version)}>回退到这一版</Button>
            </li>
          ))}
        </ul>
      </section>
      {rollback != null && (
        <ConfirmDialog title={`回退到第 ${rollback} 版`} onClose={() => setRollback(null)}>
          <p className="text-sm">回退会写成新的一版，当前内容还在历史里。</p>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setRollback(null)}>取消</Button>
            <Button type="button" disabled={busy} onClick={() => { const version = rollback; setRollback(null); save(() => intentTypesRollback({ key: type.key, version })); }}>回退到这一版</Button>
          </div>
        </ConfirmDialog>
      )}
    </form>
  );
}

function ProjectTemplates({
  type, projects, workflows, busy, onSave,
}: {
  type: IntentType; projects: Project[]; workflows: Workflow[]; busy: boolean;
  onSave: (projectId: string, workflowKey: string | null) => void;
}) {
  const [projectId, setProjectId] = useState("");
  const [workflowKey, setWorkflowKey] = useState("");
  const taken = new Set(type.projects.map((item) => item.project_id));
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">项目模板</h3>
      <ul className="space-y-2">
        {type.projects.map((item) => (
          <li key={item.project_id} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="min-w-0 flex-1">{item.project_name}</span>
            <span className="text-muted-foreground">{item.workflow_key ?? "无"}</span>
            <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => onSave(item.project_id, null)}>清除</Button>
          </li>
        ))}
      </ul>
      <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
        <select aria-label="模板项目" value={projectId} onChange={(e) => setProjectId(e.target.value)} className={SELECT}>
          <option value="">选择项目</option>
          {projects.filter((item) => !taken.has(item.project_id)).map((item) => <option key={item.project_id} value={item.project_id}>{item.name}</option>)}
        </select>
        <select aria-label="模板工作流" value={workflowKey} onChange={(e) => setWorkflowKey(e.target.value)} className={SELECT}>
          <option value="">无</option>
          {workflows.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
        </select>
        <Button type="button" size="sm" variant="outline" disabled={busy || !projectId} onClick={() => onSave(projectId, workflowKey || null)}>添加</Button>
      </div>
    </div>
  );
}

function BindingsTab() {
  const [query, setQuery] = useState("");
  const [bindings, setBindings] = useState<IntentBinding[] | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [chatId, setChatId] = useState("");
  const [projectId, setProjectId] = useState("");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    Promise.all([intentBindingsList(query.trim() ? { query: query.trim() } : {}), fetchProjectsList()]).then(([page, projectPage]) => {
      if (!live) return;
      setBindings(page.bindings);
      setProjects(projectPage.projects);
      setError("");
      setConflict("");
    }, (err: unknown) => {
      if (!live) return;
      const message = conflictOf(err);
      setConflict(message);
      setError(message ? "" : errorText(err));
    }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [query, attempt]);
  const change = (action: () => Promise<{ bindings: IntentBinding[] }>) => {
    action().then((page) => { setBindings(page.bindings); setError(""); }, (err: unknown) => {
      const message = conflictOf(err);
      setConflict(message);
      setError(message ? "" : errorText(err));
    });
  };
  return (
    <div className="space-y-4">
      <Input aria-label="搜索会话" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索会话" />
      <form className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]" onSubmit={(event) => {
        event.preventDefault();
        if (!chatId.trim() || !projectId) return;
        change(() => intentBindingsPut({ chat_id: chatId.trim(), project_id: projectId }));
      }}>
        <Input aria-label="会话 id" value={chatId} onChange={(e) => setChatId(e.target.value)} placeholder="会话 id" />
        <select aria-label="绑定项目" value={projectId} onChange={(e) => setProjectId(e.target.value)} className={SELECT}>
          <option value="">选择项目</option>
          {projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.name}</option>)}
        </select>
        <Button type="submit" size="sm">添加</Button>
      </form>
      <Notice loading={loading} error={error} conflict={conflict} onRetry={() => setAttempt((n) => n + 1)}
        empty={bindings && bindings.length === 0 ? "还没有会话绑定" : ""} />
      <ul className="space-y-2">
        {bindings?.map((binding) => (
          <li key={binding.chat_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-sm">
            <span>{binding.chat_name} → {binding.project.name}</span>
            <Button type="button" size="sm" variant="ghost" onClick={() => change(() => intentBindingsDelete({ chat_id: binding.chat_id }))}>移除</Button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function AlertsTab() {
  const [senders, setSenders] = useState<AlertSender[] | null>(null);
  const [candidates, setCandidates] = useState<AlertSender[]>([]);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    intentAlertSendersList().then((page) => {
      if (!live) return;
      setSenders(page.senders);
      setCandidates(page.candidates);
      setError("");
      setConflict("");
    }, (err: unknown) => {
      if (!live) return;
      const message = conflictOf(err);
      setConflict(message);
      setError(message ? "" : errorText(err));
    }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [attempt]);
  const change = (action: () => Promise<{ senders: AlertSender[] }>) => {
    action().then(() => setAttempt((n) => n + 1), (err: unknown) => {
      const message = conflictOf(err);
      setConflict(message);
      setError(message ? "" : errorText(err));
    });
  };
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">只用于识别「监控异常排查」</p>
      <Notice loading={loading} error={error} conflict={conflict} onRetry={() => setAttempt((n) => n + 1)}
        empty={senders && senders.length === 0 && candidates.length === 0 ? "还没有告警机器人" : ""} />
      {senders && senders.length > 0 && (
        <ul className="space-y-2">
          {senders.map((sender) => (
            <li key={sender.app_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-sm">
              <span className="font-mono text-xs">{sender.app_id}</span>
              <span className="text-muted-foreground">{sender.chats.join("、") || "—"}</span>
              <span className="tabular-nums">{sender.messages_30d}</span>
              <Button type="button" size="sm" variant="ghost" onClick={() => change(() => intentAlertSendersDelete({ app_id: sender.app_id }))}>移除</Button>
            </li>
          ))}
        </ul>
      )}
      {candidates.length > 0 && (
        <ul className="space-y-2">
          {candidates.map((sender) => (
            <li key={sender.app_id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="font-mono text-xs">{sender.app_id}</span>
              <span className="text-muted-foreground">{sender.chats.join("、")} · {sender.messages_30d}</span>
              <Button type="button" size="sm" variant="outline" onClick={() => change(() => intentAlertSendersPut({ app_id: sender.app_id }))}>加入白名单</Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function GroupsTab({ intentsEnabled }: { intentsEnabled: boolean }) {
  const returnFocus = useRef<HTMLElement | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const [page, setPage] = useState<FollowedGroups | null>(null);
  const [query, setQuery] = useState("");
  const [chats, setChats] = useState<LeadChat[]>([]);
  const [pending, setPending] = useState<FollowedGroupPreview["group"] | null>(null);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState("");
  const [searchError, setSearchError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [recognitions, setRecognitions] = useState<IntentRecognitions | null>(null);
  const [recognitionError, setRecognitionError] = useState("");
  const [recognition, setRecognition] = useState<{ chat_id: string; name: string; since: string; until: string } | null>(null);
  const [recognitionPreview, setRecognitionPreview] = useState<IntentRecognitionPreview | null>(null);
  const running = recognitions?.recognitions.some((item) => item.state === "running") ?? false;
  useEffect(() => {
    let live = true;
    const load = () => intentRecognitionsList().then((next) => {
      if (live) { setRecognitions(next); setRecognitionError(""); }
    }, (err: unknown) => { if (live) setRecognitionError(errorText(err)); });
    void load();
    const timer = running ? window.setInterval(() => void load(), 5000) : null;
    return () => { live = false; if (timer !== null) window.clearInterval(timer); };
  }, [attempt, running]);
  useEffect(() => {
    let live = true;
    intentGroupReadsList().then((next) => {
      if (!live) return;
      setPage(next);
      setError("");
      setConflict("");
    }, (err: unknown) => {
      if (!live) return;
      const message = conflictOf(err);
      setConflict(message);
      setError(message ? "" : errorText(err));
    }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [attempt]);
  useEffect(() => {
    let live = true;
    leadsChats({ query: query.trim() }).then((next) => {
      if (live) {
        setChats(next.chats.filter((chat) => chat.kind === "group" || chat.kind === "topic"));
        setSearchError("");
      }
    }, (err: unknown) => { if (live) setSearchError(errorText(err)); });
    return () => { live = false; };
  }, [query, attempt]);
  const change = async (action: () => Promise<FollowedGroups>) => {
    setBusy(true);
    try {
      setPage(await action());
      if (pending) searchInput.current?.focus();
      setPending(null);
      setError("");
      setConflict("");
    } catch (err) {
      const message = conflictOf(err);
      setConflict(message);
      setError(message ? "" : errorText(err));
    } finally { setBusy(false); }
  };
  const preview = async (chatId: string) => {
    returnFocus.current = document.activeElement as HTMLElement | null;
    setBusy(true);
    try {
      setPending((await intentGroupReadsPreview({ chat_id: chatId })).group);
      setError("");
      setConflict("");
    } catch (err) {
      const message = conflictOf(err);
      setConflict(message);
      setError(message ? "" : errorText(err));
    } finally { setBusy(false); }
  };
  const closePreview = () => {
    if (busy) return;
    setPending(null);
    returnFocus.current?.focus();
  };
  const handleChooseRecognition = (chat: { chat_id: string; name: string }, until: Date, skipped = false) => {
    returnFocus.current = document.activeElement as HTMLElement | null;
    const since = new Date(until.getTime() - (skipped ? 0 : 7 * 86400000));
    if (skipped) since.setHours(0, 0, 0, 0);
    setRecognition({ ...chat, since: localDateTime(since), until: localDateTime(until) });
    setRecognitionPreview(null);
    setRecognitionError("");
  };
  const closeRecognition = () => {
    if (busy) return;
    setRecognition(null);
    setRecognitionPreview(null);
    returnFocus.current?.focus();
  };
  const recognize = async (start: boolean) => {
    if (!recognition) return;
    setBusy(true);
    try {
      if (start && recognitionPreview) {
        const { recognition: record } = await intentRecognitionsStart({ chat_id: recognition.chat_id,
          since: recognitionPreview.range_from, until: recognitionPreview.range_to });
        setRecognitions((current) => current && { ...current, recognitions: [record, ...current.recognitions] });
        setRecognition(null);
        setRecognitionPreview(null);
        returnFocus.current?.focus();
      } else {
        const since = new Date(recognition.since);
        const until = new Date(recognition.until);
        if (!Number.isFinite(since.getTime()) || !Number.isFinite(until.getTime()) || since >= until
          || until.getTime() > Date.now() || until.getTime() - since.getTime() > 30 * 86400000) {
          throw new Error("请选择不晚于现在、起点早于终点且跨度不超过 30 天的范围");
        }
        setRecognitionPreview(await intentRecognitionsPreview({ chat_id: recognition.chat_id,
          since: since.toISOString(), until: until.toISOString() }));
      }
      setRecognitionError("");
    } catch (err) { setRecognitionError(errorText(err)); }
    finally { setBusy(false); }
  };
  const toggleRecognition = async (enabled: boolean) => {
    setBusy(true);
    try { setRecognitions(await intentRecognitionsSettingsPut({ enabled })); setRecognitionError(""); }
    catch (err) { setRecognitionError(errorText(err)); }
    finally { setBusy(false); }
  };
  const recognitionDisabled = busy || !recognitions?.enabled || !intentsEnabled;
  const followed = new Set(page?.groups.map((group) => group.chat_id));
  return (
    <div className="space-y-4">
      <Notice loading={loading} error={error} conflict={conflict} onRetry={() => { setLoading(true); setAttempt((n) => n + 1); }} empty="" />
      {page && <>
        <div className="flex items-center justify-between gap-3">
          <div className="space-y-1">
            <h2 className="text-sm font-medium">持续识别群里的开发需求</h2>
            <p className="text-xs text-muted-foreground">只读名单上的群，每次最多 {page.run_max} 条，每个群每天最多 {page.day_max} 条；最多关注 {page.max_groups} 个群。</p>
          </div>
          <Switch label="持续识别群需求" checked={page.enabled} disabled={busy || !page.delivery_enabled}
            onChange={(enabled) => void change(() => intentGroupReadsSettingsPut({ enabled }))} />
        </div>
        {!page.delivery_enabled && <p className="text-sm text-muted-foreground">先在 Settings 打开送达层：名单上的群产生的意图要按非紧急类限量，超出的进晨报</p>}
        <div className="flex items-center justify-between gap-3">
          <div className="space-y-1">
            <h2 className="text-sm font-medium">按请求识别一次</h2>
            <p className="text-xs text-muted-foreground">先预览，再确认；默认最近 7 天，最多 30 天、2,000 条消息和 $1。结果留在识别记录，不逐条推送。</p>
          </div>
          <Switch label="启用识别一次" checked={recognitions?.enabled ?? false} disabled={busy || !recognitions}
            onChange={(enabled) => void toggleRecognition(enabled)} />
        </div>
        {recognitionError && !recognition && <p role="alert" className="text-sm text-destructive">{recognitionError}<Button type="button" size="sm" variant="outline" onClick={() => setAttempt((n) => n + 1)}>重试</Button></p>}
        {page.groups.length === 0 && <p role="status" className="text-sm text-muted-foreground">还没有关注的群</p>}
        <ul className="space-y-2" aria-label="关注的群">
          {page.groups.map((group) => <li key={group.chat_id} className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border px-3 py-2 text-sm">
            <div className="min-w-0 flex-1 space-y-1">
              <div className="flex flex-wrap items-center gap-2"><span className="break-words font-medium">{group.name}</span>
                <Badge variant="outline">{group.kind === "topic" ? "话题群" : "群"}</Badge>
                {!group.readable && <Badge variant="outline">已排除，不读</Badge>}
              </div>
              <p className="text-xs text-muted-foreground">加入：{new Date(group.added_at).toLocaleString("zh-CN")} · 开始：{new Date(group.read_from).toLocaleString("zh-CN")}</p>
              <p className="text-xs text-muted-foreground tabular-nums">今天读了 {group.read_today} 条，跳过 {group.skipped_today} 条 · 24 小时 {group.messages_24h} 条消息 · 7 天 {group.intents_7d} 条意图</p>
            </div>
            <div className="flex gap-2">
              <Button type="button" size="sm" variant="outline" disabled={recognitionDisabled || !group.readable}
                onClick={() => handleChooseRecognition(group, new Date(), group.skipped_today > 0)}>识别一次</Button>
              <Button type="button" size="sm" variant="ghost" disabled={busy}
              onClick={() => void change(() => intentGroupReadsDelete({ chat_id: group.chat_id }))}>撤销</Button>
            </div>
          </li>)}
        </ul>
        <div className="space-y-2">
          <Input ref={searchInput} aria-label="搜索群" value={query} onChange={(event) => { setChats([]); setSearchError(""); setQuery(event.target.value); }} placeholder="搜索要关注的群" />
          {searchError && <p role="alert" className="text-sm text-destructive">{searchError}</p>}
          <ul className="space-y-2" aria-label="群搜索结果">
            {chats.filter((chat) => !followed.has(chat.chat_id)).map((chat) => <li key={chat.chat_id} className="flex flex-wrap items-center justify-between gap-3 text-sm">
              <span className="min-w-0 break-words">{chat.name} · {chat.kind === "topic" ? "话题群" : "群"}</span>
              <div className="flex gap-2">
                <Button type="button" size="sm" variant="outline" disabled={recognitionDisabled}
                  onClick={() => handleChooseRecognition(chat, new Date())}>识别一次</Button>
                <Button type="button" size="sm" variant="outline" disabled={busy || page.groups.length >= page.max_groups}
                onClick={() => void preview(chat.chat_id)}>关注</Button>
              </div>
            </li>)}
          </ul>
        </div>
        <section className="space-y-2" aria-label="识别记录">
          <h2 className="text-sm font-medium">识别记录</h2>
          {recognitions?.recognitions.length === 0 && <p className="text-sm text-muted-foreground">还没有识别记录</p>}
          {recognitions?.recognitions.map((record) => <article key={record.id} className="space-y-2 rounded-md border border-border p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2"><span className="font-mono">{record.id}</span><span>{record.name}</span>
              <Badge variant="outline">{({ running: "识别中", done: "已完成", failed: "失败" })[record.state]}</Badge></div>
            <p className="text-xs text-muted-foreground">{new Date(record.range_from).toLocaleString("zh-CN")} – {new Date(record.range_to).toLocaleString("zh-CN")} · 读了 {record.events_read} / {record.events_in_range} 条 · ${record.cost_usd.toFixed(4)}</p>
            {record.truncated && <p className="text-xs text-muted-foreground">{record.truncated === "messages" ? "消息超过上限，只识别最新 2,000 条" : "已到 $1 花费上限，剩余消息未识别"}</p>}
            {record.error && <p className="text-sm text-destructive">{record.error}</p>}
            {record.state === "done" && record.intents.length === 0 && <p className="text-sm text-muted-foreground">没有识别出开发需求</p>}
            <ul className="space-y-1">{record.intents.map((item) => <li key={item.id} className="break-words">
              {item.id} · {item.type.name} · {item.title} · {label(INTENT_STATE_ZH, item.state)}
            </li>)}</ul>
          </article>)}
        </section>
        {recognition && <ConfirmDialog title={`识别一次：《${recognition.name}》`} onClose={closeRecognition}>
          <p className="text-sm text-muted-foreground">只读范围内已采集的人发消息；不改关注名单和游标。</p>
          <div className="grid gap-3 sm:grid-cols-2">
            {(["since", "until"] as const).map((field) => <label key={field} className={LABEL}>
              {field === "since" ? "范围起点" : "范围终点"}
              <Input type="datetime-local" aria-label={field === "since" ? "范围起点" : "范围终点"} value={recognition[field]}
                disabled={busy}
                onChange={(event) => { setRecognition({ ...recognition, [field]: event.target.value }); setRecognitionPreview(null); }} />
            </label>)}
          </div>
          {recognitionPreview && <div className="space-y-2 text-sm">
            <p>{recognitionPreview.events} 条人发消息，约 ${recognitionPreview.estimate_usd[0]}–${recognitionPreview.estimate_usd[1]}，上限 $1。</p>
            {recognitionPreview.truncated === "messages" && <p>超过 2,000 条，只识别最新 2,000 条；实际起点：{new Date(recognitionPreview.effective_from).toLocaleString("zh-CN")}。</p>}
            <p>结果作为意图入库，完成后显示在下面的识别记录，不逐条推送。</p>
          </div>}
          {recognitionError && <p role="alert" className="text-sm text-destructive">{recognitionError}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={closeRecognition}>取消</Button>
            <Button type="button" size="sm" disabled={busy} onClick={() => void recognize(!!recognitionPreview)}>
              {recognitionPreview ? "确认识别" : "预览条数和花费"}
            </Button>
          </div>
        </ConfirmDialog>}
        {pending && <ConfirmDialog title="关注这个群" onClose={closePreview}>
          <p className="text-sm">从现在起读「{pending.name}」里人发的消息；加入之前的、机器人和导入的都不读。最近 24 小时 {pending.messages_24h} 条，每天约 ${pending.estimate_usd[0]}–${pending.estimate_usd[1]}；每个群每天最多 {page.day_max} 条。推送走送达层，只发你的私聊。</p>
          {(error || conflict) && <p role="alert" className="text-sm text-destructive">{error || conflict}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={closePreview}>取消</Button>
            <Button type="button" size="sm" disabled={busy} onClick={() => void change(() => intentGroupReadsPut({ chat_id: pending.chat_id }))}>确认关注</Button>
          </div>
        </ConfirmDialog>}
      </>}
    </div>
  );
}

function localDateTime(date: Date): string {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
