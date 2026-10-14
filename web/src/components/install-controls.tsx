import { Badge } from "@/components/ui/badge";
import type { AgentRole, CatalogItem } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * The roles an install names, one checkbox each. An install names at least one role, so the last checked one is
 * disabled and `lastHint` says what to do instead.
 */
export function RoleChecks({
  roles,
  value,
  onChange,
  disabled,
  label,
  lastHint,
}: {
  /** The roles the scope allows, in order. */
  roles: AgentRole[];
  value: AgentRole[];
  onChange: (roles: AgentRole[]) => void;
  disabled?: boolean;
  /** The group's accessible name, e.g. "Global roles of alpha". */
  label: string;
  lastHint: string;
}) {
  const checked = new Set(value);
  return (
    <div role="group" aria-label={label} className="flex flex-wrap items-center gap-x-3">
      {roles.map((role) => {
        const on = checked.has(role);
        const last = on && value.length === 1;
        return (
          <label
            key={role}
            title={last ? lastHint : undefined}
            className={cn(
              "inline-flex min-h-8 cursor-pointer items-center gap-1.5 text-xs capitalize",
              (disabled || last) && "cursor-not-allowed opacity-60",
            )}
          >
            <input
              type="checkbox"
              className="h-4 w-4 accent-primary"
              checked={on}
              disabled={disabled || last}
              title={last ? lastHint : undefined}
              onChange={(e) => onChange(roles.filter((r) => (r === role ? e.target.checked : checked.has(r))))}
            />
            {role}
          </label>
        );
      })}
    </div>
  );
}

/** How an item came into the catalog; a project's skill names the project, or "written here" inside it. */
export function OriginTag({ item, projectId }: { item: CatalogItem; projectId?: string }) {
  const owner = item.owner_project_name ?? item.owner_project_id ?? "a project";
  const text =
    item.origin !== "project"
      ? item.origin
      : projectId === undefined
        ? `project · ${owner}`
        : item.owner_project_id === projectId
          ? "written here"
          : `from ${owner}`;
  return (
    <Badge variant="outline" className="py-0 font-sans text-[10px] font-normal">
      {text}
      {item.modified && " · modified"}
    </Badge>
  );
}
