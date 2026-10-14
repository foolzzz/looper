import { ArrowLeftRight, Bot, Cloud, Cpu, MessageCircle, Puzzle, Users, type LucideIcon } from "lucide-react";

/**
 * An Agent Profile: a row of `agent_profiles` (the API's `/api/agent-profiles`), a runtime, an LLM provider and a
 * model with a default effort. Its name, plural, route and help text live here only.
 */
export const AGENT_PROFILE = {
  name: "Agent Profile",
  plural: "Agent Profiles",
  slug: "profiles",
  help: "A runtime, an LLM provider and a model, with a default effort. Roles, projects and tasks select one.",
} as const;

/** The Settings pages, in the order the sidebar lists them; each is #settings/<id>. */
export const SETTINGS_PAGES = [
  { id: "providers", label: "LLM Providers", icon: Cloud },
  { id: "runtimes", label: "Agent Runtimes", icon: Cpu },
  { id: AGENT_PROFILE.slug, label: AGENT_PROFILE.plural, icon: Bot },
  { id: "roles", label: "Roles", icon: Users },
  { id: "catalog", label: "Skills & MCP", icon: Puzzle },
  { id: "channels", label: "Channels", icon: MessageCircle },
  { id: "transfer", label: "Import / Export", icon: ArrowLeftRight },
] as const satisfies readonly { id: string; label: string; icon: LucideIcon }[];

export type SettingsPageId = (typeof SETTINGS_PAGES)[number]["id"];

/** Where #settings, and the links from before the pages split, go. */
export const DEFAULT_SETTINGS_PAGE: SettingsPageId = "roles";

export function isSettingsPage(id: string): id is SettingsPageId {
  return SETTINGS_PAGES.some((page) => page.id === id);
}

export function settingsPageLabel(id: SettingsPageId): string {
  return SETTINGS_PAGES.find((page) => page.id === id)?.label ?? id;
}

/** The link to a Settings page, or to something on it: #settings/<page>[/<sub>]. */
export function settingsHref(page: SettingsPageId, sub = ""): string {
  return `#settings/${page}${sub ? `/${sub}` : ""}`;
}
