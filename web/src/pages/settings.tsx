import { AgentProfilesSection } from "@/components/agent-profiles";
import { CatalogCard } from "@/components/catalog-card";
import { ChatSettingsSection } from "@/components/chat-settings";
import { ChannelsSection } from "@/components/channel-settings";
import { ConfigTransferCard } from "@/components/config-transfer";
import { ManagersSection } from "@/components/managers-settings";
import { ProvidersSection } from "@/components/provider-settings";
import { RolesSection } from "@/components/role-settings";
import { RuntimesSection } from "@/components/runtime-cards";
import { SchedulerSection } from "@/components/scheduler-settings";
import { VaultSettingsSection } from "@/components/vault-settings";
import {
  AGENT_PROFILE,
  DEFAULT_SETTINGS_PAGE,
  isSettingsPage,
  settingsPageLabel,
  type SettingsPageId,
} from "@/lib/settings-pages";

/** What each Settings page is for, under its title; the Roles page writes its own, as it names the roles. */
const DESCRIPTIONS: Record<Exclude<SettingsPageId, "roles">, React.ReactNode> = {
  providers:
    "Where agents' requests go: each provider's protocols, endpoint, sign-in (a runtime's own login, or an API key " +
    "stored masked), model catalog with prices, extra environment and concurrency limit.",
  runtimes:
    "The agent runtimes this version can run, as found on the server's PATH. A runtime's capabilities decide the " +
    "roles it can run. Runtimes are built in; a different binary comes from the server's PATH.",
  [AGENT_PROFILE.slug]: AGENT_PROFILE.help,
  managers:
    "Managers you talk to, each with its own duty, session and spend. Task templates set what a Manager's tasks run with.",
  catalog:
    "Plugins, skills and MCP servers: import, upload, take from git or write them, then install them globally or " +
    "in a project, for the roles that load them.",
  channels:
    "Connect external messaging platforms. Each channel's card says where its credential comes from; saved " +
    "changes take effect after the server restarts. Each channel passes on only the senders it lists. Each Manager can have its own bots; a bot's Manager is set when it is added.",
  chat: "How the chat with the Manager behaves, on the dashboard and on the channels.",
  scheduler:
    "System-wide limits of schedules: how close two fires may be, and the run timeout of schedules that follow the default.",
  vault:
    "A local folder of Markdown notes, such as an Obsidian vault. The agents of projects that turn it on read every " +
    "note and write only in its writable folders; each write is a git commit in the vault.",
  transfer:
    "One file with the role settings, the providers and Agent Profiles, the catalog with its files and MCP servers, " +
    "global installs, channels and the scheduler's settings. Managers, their templates and their bots are not included.",
};

/** One Settings page (#settings/<page>[/<sub>]); the sidebar's Settings group lists them. */
export function SettingsPage({ sub }: { sub: string }) {
  const [first, ...rest] = sub.split("/");
  const page: SettingsPageId = isSettingsPage(first) ? first : DEFAULT_SETTINGS_PAGE;
  const inner = rest.join("/");

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-bold">{settingsPageLabel(page)}</h1>
        {page !== "roles" && <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{DESCRIPTIONS[page]}</p>}
      </div>
      {page === "providers" && <ProvidersSection adding={inner === "new"} />}
      {page === "runtimes" && <RuntimesSection />}
      {page === AGENT_PROFILE.slug && <AgentProfilesSection />}
      {page === "roles" && <RolesSection sub={inner} />}
      {page === "managers" && <ManagersSection />}
      {page === "catalog" && <CatalogCard />}
      {page === "channels" && <ChannelsSection />}
      {page === "chat" && <ChatSettingsSection />}
      {page === "scheduler" && <SchedulerSection />}
      {page === "vault" && <VaultSettingsSection />}
      {page === "transfer" && <ConfigTransferCard />}
    </div>
  );
}
