import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { SELECT } from "@/components/registry-ui";
import { fetchAgentSettings, fetchCatalog, type CatalogItem, type ManagerInfo } from "@/lib/api";
import {
  deleteManagerInstall, fetchManagerInstalls, putManagerInstall, type ManagerInstallsSnapshot,
} from "@/lib/manager-installs-api";
import { settingsHref } from "@/lib/settings-pages";
import { errorText } from "@/lib/use-action";
import { useRegistry } from "@/lib/use-registry";

function RuntimeHint({ manager }: { manager: ManagerInfo }) {
  const { registry, error } = useRegistry();
  const [sharedProfile, setSharedProfile] = useState<string | null>(null);
  const [settingsError, setSettingsError] = useState("");
  useEffect(() => {
    if (manager.profile_id) return;
    let alive = true;
    fetchAgentSettings().then((settings) => {
      if (alive) setSharedProfile(settings.roles.manager.effective.profile.value.profile_id);
    }).catch((err: unknown) => { if (alive) setSettingsError(errorText(err)); });
    return () => { alive = false; };
  }, [manager.profile_id]);
  const profile = registry?.profiles.find((p) => p.profile_id === (manager.profile_id ?? sharedProfile));
  const runtime = registry?.runtimes?.find((r) => r.runtime_id === profile?.runtime_id);
  return (
    <div className="space-y-1 text-xs text-muted-foreground">
      <p>
        {error || settingsError ? `Runtime capabilities unavailable: ${error || settingsError}`
          : !profile ? "Runtime capabilities have not been resolved."
          : profile.runtime_type === "pi" ? "Pi does not load catalog plugins or skills; they are recorded as skipped."
          : `${profile.runtime_name}: ${runtime?.capabilities.includes("claude_plugins")
            ? "supports Claude plugin loading." : "only runtime-supported contents load."}`}
        {" "}<a className="underline" href={settingsHref("profiles")}>Agent Profiles</a>
        {" · "}<a className="underline" href="#runs/manager">Runs: loaded contents and runtime catalog skips</a>
      </p>
      {runtime?.probe_error && <p role="alert">{runtime.probe_error}</p>}
      {profile?.warnings.map((warning) => <p key={warning}>{warning}</p>)}
    </div>
  );
}

/** A target change remounts local state; late reads and writes stay with their original Manager. */
export function ManagerInstalls(props: { manager: ManagerInfo; hasScenarios: boolean }) {
  return <InstallDetails key={props.manager.id} {...props} />;
}

function InstallDetails({ manager, hasScenarios }: { manager: ManagerInfo; hasScenarios: boolean }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<ManagerInstallsSnapshot | null>(null);
  const [catalog, setCatalog] = useState<CatalogItem[] | null>(null);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [revision, setRevision] = useState(0);
  const active = useRef(false);
  const pending = useRef(false);
  const picker = useRef<HTMLSelectElement>(null);
  const disclosure = useRef<HTMLButtonElement>(null);
  const fallbackFocus = useRef<HTMLElement | null>(null);
  const restoreFocus = useRef<"picker" | HTMLElement | null>(null);

  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    fetchManagerInstalls(manager.id, controller.signal).then((snapshot) => {
      if (current) {
        setView(snapshot);
        setSelected((draft) => snapshot.installs.some((row) => !row.inherited && row.item_id === draft) ? "" : draft);
      }
    }).catch((err: unknown) => { if (current) setError(errorText(err)); })
      .finally(() => { if (current) setBusy(false); });
    return () => { current = false; controller.abort(); };
  }, [manager.id, revision]);

  useEffect(() => {
    if (!open) return;
    let current = true;
    fetchCatalog().then((result) => { if (current) setCatalog(result.items); })
      .catch((err: unknown) => { if (current) setError(errorText(err)); });
    return () => { current = false; };
  }, [open, revision]);

  const own = view?.installs.filter((row) => !row.inherited) ?? [];
  const visible = !manager.builtin || hasScenarios || own.length > 0 || !!error;
  useEffect(() => {
    if (busy || !restoreFocus.current) return;
    if (!visible) {
      fallbackFocus.current?.focus();
    } else if (restoreFocus.current === "picker" && picker.current && !picker.current.disabled) {
      picker.current.focus();
    } else if (restoreFocus.current instanceof HTMLElement && restoreFocus.current.isConnected) {
      restoreFocus.current.focus();
    } else {
      disclosure.current?.focus();
    }
    restoreFocus.current = null;
  }, [busy, visible]);

  const mutate = async (operation: () => Promise<ManagerInstallsSnapshot>, adding = false, focusPicker = true) => {
    if (pending.current) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    fallbackFocus.current = disclosure.current?.closest("li")?.querySelector<HTMLButtonElement>(
      `button[aria-label=${CSS.escape(`Edit Manager ${manager.name}`)}]`) ?? null;
    pending.current = true;
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      await operation();
      // Read back committed state; a request count or an optimistic switch is not confirmation.
      const snapshot = await fetchManagerInstalls(manager.id);
      if (!active.current) return;
      setView(snapshot);
      if (adding) setSelected("");
      setSaved(true);
    } catch (err) {
      if (active.current) setError(errorText(err));
    } finally {
      pending.current = false;
      if (active.current) {
        restoreFocus.current = focusPicker ? "picker" : previousFocus;
        setBusy(false);
      }
    }
  };

  if (!visible) return null;
  const available = (catalog ?? []).filter((item) => (item.kind === "plugin" || item.kind === "skill")
    && item.origin !== "project" && !item.owner_project_id && !own.some((row) => row.item_id === item.item_id));
  const retry = () => {
    setSaved(false);
    setBusy(true);
    setError("");
    if (open) setCatalog(null);
    setRevision((value) => value + 1);
  };
  return (
    <section aria-label={`Plugins and skills for ${manager.name}`} className="mt-3 min-w-0 space-y-2">
      <Button ref={disclosure} type="button" size="sm" variant="ghost" aria-expanded={open}
        aria-controls={`${id}-details`} onClick={() => {
          if (!open) setCatalog(null);
          setOpen(!open);
        }} disabled={busy && !!view}>
        Plugins and skills
      </Button>
      {open && (
        <div id={`${id}-details`} className="space-y-3 rounded-md border border-border p-3">
          <p className="text-xs text-muted-foreground">
            Changes take effect next turn. An active turn completes with its current installs.
            {" "}Disabling or removing an addition does not mask an inherited global install.
            {" "}An item inherited and added here loads once.
          </p>
          <RuntimeHint key={manager.profile_id ?? "shared"} manager={manager} />
          <a href={settingsHref("catalog")} className="text-xs underline">Settings → Skills &amp; MCP</a>
          {!catalog && !error && <p role="status" className="text-xs text-muted-foreground">Loading catalog…</p>}
          {busy && <p role="status" className="text-xs text-muted-foreground">Loading or saving installs…</p>}
          {error && <div className="space-y-2">
            <p role="alert" className="break-words text-xs text-destructive">{error}</p>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={retry}>Retry installs</Button>
          </div>}
          {saved && !error && <p role="status" className="text-xs text-muted-foreground">Saved and read back from server; applies next turn.</p>}
          {view && <>
            {!view.installs.length && <p className="text-xs text-muted-foreground">No plugins or skills installed for this Manager.</p>}
            <ul className="space-y-2">
              {view.installs.map((row) => <li key={`${row.inherited}:${row.item_id}`}
                className="flex min-w-0 flex-col gap-2 border-t border-border pt-2 sm:flex-row sm:items-center">
                <div className="min-w-0 flex-1 space-y-1">
                  <p className="break-words text-sm">{row.name} <Badge variant="outline">{row.kind}</Badge></p>
                  <p className="text-xs text-muted-foreground">
                    {row.inherited ? "Inherited globally" : "Added here"} · {row.enabled ? "Enabled" : "Disabled"}
                  </p>
                </div>
                {!row.inherited && <div className="flex shrink-0 items-center gap-3">
                  <Switch checked={row.enabled} disabled={busy} label={`Enable ${row.name} for ${manager.name}`}
                    onChange={(enabled) => void mutate(() => putManagerInstall(manager.id, row.item_id, enabled), false, false)} />
                  <Button type="button" size="sm" variant="ghost" disabled={busy}
                    aria-label={`Remove ${row.name} from ${manager.name}`}
                    onClick={() => void mutate(() => deleteManagerInstall(manager.id, row.item_id))}>Remove</Button>
                </div>}
              </li>)}
            </ul>
            <form className="flex flex-col gap-2 sm:flex-row" onSubmit={(event) => {
              event.preventDefault();
              if (selected) void mutate(() => putManagerInstall(manager.id, selected), true);
            }}>
              <label className="sr-only" htmlFor={`${id}-item`}>Add plugin or skill for {manager.name}</label>
              <select ref={picker} id={`${id}-item`} className={`${SELECT} min-w-0 flex-1`} value={selected}
                onChange={(event) => setSelected(event.target.value)} disabled={busy || !catalog}>
                <option value="">Choose a plugin or skill</option>
                {available.map((item) => <option key={item.item_id} value={item.item_id}>{item.name} · {item.kind}</option>)}
              </select>
              <Button type="submit" size="sm" variant="outline" disabled={busy || !catalog || !selected}>Add install</Button>
            </form>
            {catalog && !available.length && <p className="text-xs text-muted-foreground">No additional plugins or skills in the catalog.</p>}
          </>}
        </div>
      )}
      {!open && error && <p role="alert" className="break-words text-xs text-destructive">{error}</p>}
    </section>
  );
}
