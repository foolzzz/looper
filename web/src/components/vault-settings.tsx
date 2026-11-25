import { useEffect, useId, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { fetchResidentProfileSettings, updateResidentProfileSettings,
  fetchVaultSettings, updateVaultSettings, type VaultSettings } from "@/lib/api";
import { errorText } from "@/lib/use-action";

export function VaultSettingsSection() {
  const id = useId();
  const [settings, setSettings] = useState<VaultSettings | null>(null);
  const [loadError, setLoadError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [path, setPath] = useState("");
  const [writable, setWritable] = useState("");
  const [rulesFile, setRulesFile] = useState("");
  const [profileSource, setProfileSource] = useState("关于我.md");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const apply = (value: VaultSettings) => {
    setSettings(value);
    setPath(value.path ?? "");
    setWritable(value.writable.join(", "));
    setRulesFile(value.rules_file ?? "");
    setProfileSource(value.profile_source ?? "关于我.md");
  };

  useEffect(() => {
    let alive = true;
    fetchVaultSettings().then((value) => {
      if (alive) apply(value);
    }).catch((err: unknown) => {
      if (alive) setLoadError(errorText(err));
    });
    return () => { alive = false; };
  }, [attempt]);

  useEffect(() => {
    if (!saved) return;
    const timer = setTimeout(() => setSaved(false), 2000);
    return () => clearTimeout(timer);
  }, [saved]);

  const save = async (body: Parameters<typeof updateVaultSettings>[0]) => {
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      apply(await updateVaultSettings(body));
      setSaved(true);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const Icon = settings?.problem ? AlertTriangle : settings?.configured ? CheckCircle2 : CircleOff;
  const status = settings?.problem || (!settings?.configured
    ? "Not configured: agents have no vault."
    : settings.git
      ? "Configured: a git repository; each write is a commit."
      : "Configured: not a git repository; writes are not committed.");

  return (
    <Card className="min-w-0 p-5">
      {loadError ? (
        <div className="space-y-3">
          <p role="alert" className="break-words text-sm text-destructive">{loadError}</p>
          <Button variant="outline" onClick={() => { setLoadError(""); setAttempt((n) => n + 1); }}>
            Retry
          </Button>
        </div>
      ) : !settings ? (
        <p role="status" className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <form className="space-y-4" onSubmit={(event) => {
          event.preventDefault();
          if (!saving) void save({
            path: path.trim() || null,
            writable: writable.split(",").map((folder) => folder.trim()).filter(Boolean),
            rules_file: rulesFile.trim() || null,
            // Omit the default so an older vault without a profile can still be saved.
            ...(profileSource.trim() && profileSource.trim() !== "关于我.md"
              ? { profile_source: profileSource.trim() } : {}),
            ...(settings.manager_read ? { manager_read: true } : {}),
            ...(path.trim() && settings.worktree_root ? { worktree_root: settings.worktree_root } : {}),
          });
        }}>
          <p role={settings.problem ? "alert" : "status"}
             className={`flex items-start gap-2 break-words text-sm ${settings.problem ? "text-destructive" : "text-muted-foreground"}`}>
            <Icon aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="min-w-0">{status}</span>
          </p>
          <div className="space-y-1.5">
            <label htmlFor={`${id}-path`} className="text-xs font-medium text-muted-foreground">Vault folder</label>
            <Input id={`${id}-path`} value={path} onChange={(event) => setPath(event.target.value)}
                   placeholder="/absolute/path/to/vault" disabled={saving} aria-describedby={`${id}-path-help`} />
            <p id={`${id}-path-help`} className="text-xs text-muted-foreground">Absolute path to the vault folder on the server.</p>
          </div>
          <div className="space-y-1.5">
            <label htmlFor={`${id}-writable`} className="text-xs font-medium text-muted-foreground">Writable folders</label>
            <Input id={`${id}-writable`} value={writable} onChange={(event) => setWritable(event.target.value)}
                   placeholder="codex-mem" disabled={saving} aria-describedby={`${id}-writable-help`} />
            <p id={`${id}-writable-help`} className="text-xs text-muted-foreground">
              Folders inside the vault that agents may write, comma-separated. Empty: read only. Personal notes stay out of this list.
            </p>
          </div>
          <div className="space-y-1.5">
            <label htmlFor={`${id}-rules`} className="text-xs font-medium text-muted-foreground">Rules file</label>
            <Input id={`${id}-rules`} value={rulesFile} onChange={(event) => setRulesFile(event.target.value)}
                   placeholder="codex-mem/AGENTS.md" disabled={saving} aria-describedby={`${id}-rules-help`} />
            <p id={`${id}-rules-help`} className="text-xs text-muted-foreground">
              A note inside the vault with the rules the Planner follows in its closeout when a task ends. Optional.
            </p>
          </div>
          <div className="space-y-1.5">
            <label htmlFor={`${id}-profile`} className="text-xs font-medium text-muted-foreground">画像文件</label>
            <Input id={`${id}-profile`} value={profileSource} onChange={(event) => setProfileSource(event.target.value)}
                   placeholder="关于我.md" disabled={saving} aria-describedby={`${id}-profile-help`} />
            <p id={`${id}-profile-help`} className="text-xs text-muted-foreground">
              相对知识库根目录的一份 Markdown 笔记，例如 notes/profile.md。留空使用 关于我.md。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
            {settings.configured && <Button type="button" variant="outline" disabled={saving}
                                           onClick={() => void save({ path: null })}>Turn off</Button>}
            {saved && <span role="status" className="text-sm text-muted-foreground">Saved.</span>}
          </div>
          {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
        </form>
      )}
      {settings && !loadError && <ResidentProfileSwitch />}
    </Card>
  );
}

function ResidentProfileSwitch() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let alive = true;
    fetchResidentProfileSettings().then((value) => {
      if (alive) { setEnabled(value.enabled); setError(""); }
    }).catch((err: unknown) => { if (alive) setError(errorText(err)); });
    return () => { alive = false; };
  }, [attempt]);
  const save = async (next: boolean) => {
    if (!confirm(next ? "开启管家画像？这会在你自己的会话中加入画像内容。" : "关闭管家画像？后续回合将不再加入画像。")) return;
    setSaving(true);
    setError("");
    try {
      setEnabled((await updateResidentProfileSettings(next)).enabled);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };
  return <div className="mt-5 space-y-2 border-t border-border pt-4">
    <div className="flex items-center justify-between gap-3">
      <div className="space-y-1">
        <p className="text-sm font-medium">管家常驻画像</p>
        <p className="text-xs text-muted-foreground">默认关闭。开启后，管家在你自己的会话中使用编译后的画像。</p>
      </div>
      <Switch label="管家常驻画像" checked={enabled ?? false} disabled={enabled === null || saving}
              onChange={(next) => void save(next)} />
    </div>
    {error && <div className="space-y-2">
      <p role="alert" className="break-words text-sm text-destructive">{error}</p>
      {enabled === null && <Button type="button" variant="outline" onClick={() => setAttempt((n) => n + 1)}>重试画像设置</Button>}
    </div>}
  </div>;
}
