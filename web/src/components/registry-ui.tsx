import { useId, useState } from "react";
import { Loader2, Pencil, Plus, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/** The pieces the provider and Agent Profile lists share: a titled list with its add form, rows, and forms. */

export const SELECT = "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground";
export const LABEL = "text-xs font-medium text-muted-foreground";
export const SMALL_LABEL = "text-[11px] text-muted-foreground";

/** A titled list with an add form that opens above its rows. */
export function RegistryList({
  title,
  hint,
  addLabel,
  form,
  children,
  defaultAdding = false,
}: {
  title: string;
  hint: React.ReactNode;
  addLabel: string;
  form: (close: () => void) => React.ReactNode;
  children: React.ReactNode;
  /** Open the add form at once (a link to it: #settings/providers/new). */
  defaultAdding?: boolean;
}) {
  const id = useId();
  const [adding, setAdding] = useState(defaultAdding);
  return (
    <section aria-labelledby={`${id}-title`} className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3">
        <h3 id={`${id}-title`} className="text-sm font-semibold">{title}</h3>
        <p className="min-w-0 flex-1 text-[11px] text-muted-foreground">{hint}</p>
        <Button type="button" size="sm" variant="outline" onClick={() => setAdding(!adding)} aria-expanded={adding}>
          <Plus className="h-3.5 w-3.5" /> {addLabel}
        </Button>
      </div>
      {adding && <div className="border-t border-border bg-muted/20 px-5 py-3">{form(() => setAdding(false))}</div>}
      <ul className="divide-y divide-border border-t border-border">{children}</ul>
    </section>
  );
}

/** One registered entry: its name and badges, its actions, its details, then any error and its edit form. */
export function Row({
  title,
  actions,
  children,
  below,
  ref,
}: {
  title: React.ReactNode;
  actions: React.ReactNode;
  children: React.ReactNode;
  below: React.ReactNode;
  ref?: React.Ref<HTMLLIElement>;
}) {
  return (
    <li ref={ref} className="space-y-1 px-5 py-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {title}
        <div className="ml-auto flex flex-wrap gap-1">{actions}</div>
      </div>
      {children}
      {below}
    </li>
  );
}

export function RowName({ name, builtin, kind }: { name: string; builtin: boolean; kind?: string }) {
  return (
    <>
      <span className="break-all text-sm font-medium">{name}</span>
      {builtin && <Badge variant="outline">built-in</Badge>}
      {kind && <span className="text-xs text-muted-foreground">{kind}</span>}
    </>
  );
}

/** Edit and Delete for an entry that is not built in; `deleteBlocked` says why it cannot be deleted now. */
export function EditDelete({
  what,
  name,
  editing,
  busy,
  onEdit,
  onDelete,
  deleteBlocked,
}: {
  what: string;
  name: string;
  editing: boolean;
  busy: boolean;
  onEdit: () => void;
  onDelete: () => void;
  deleteBlocked?: string;
}) {
  return (
    <>
      <Button type="button" variant="ghost" size="sm" onClick={onEdit} aria-expanded={editing} aria-label={`Edit ${what} ${name}`}>
        <Pencil className="h-3.5 w-3.5" /> Edit
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={onDelete}
        disabled={busy || !!deleteBlocked}
        title={deleteBlocked}
        aria-label={deleteBlocked ? `Delete ${what} ${name}: ${deleteBlocked}` : `Delete ${what} ${name}`}
        className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
      >
        <Trash2 className="h-3.5 w-3.5" /> Delete
      </Button>
    </>
  );
}

/** A form's error, submit and Cancel; `onSubmit` makes the submit a plain button, for a form inside another. */
export function FormFooter({
  error,
  busy,
  submit,
  onCancel,
  onSubmit,
}: {
  error: string;
  busy: boolean;
  submit: string;
  onCancel: () => void;
  onSubmit?: () => void;
}) {
  return (
    <>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex gap-2">
        <Button type={onSubmit ? "button" : "submit"} onClick={onSubmit} size="sm" disabled={busy}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {submit}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </>
  );
}

export const EDIT_BOX = "mt-2 rounded-md border border-border bg-muted/20 p-3";

