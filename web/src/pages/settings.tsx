import { AgentProfilesSection } from "@/components/agent-profiles";
import { CatalogCard } from "@/components/catalog-card";
import { ChannelsSection } from "@/components/channel-settings";
import { ConfigTransferCard } from "@/components/config-transfer";
import { ProvidersSection } from "@/components/provider-settings";
import { RolesSection } from "@/components/role-settings";
import { RuntimesSection } from "@/components/runtime-cards";
import {
  AGENT_PROFILE,
  DEFAULT_SETTINGS_PAGE,
  isSettingsPage,
  settingsPageLabel,
  type SettingsPageId,
} from "@/lib/settings-pages";

/** What each Settings page is for, under its title. */
const DESCRIPTIONS: Record<SettingsPageId, React.ReactNode> = {
  providers:
    "Where agents' requests go: each provider's protocols, endpoint, sign-in (a runtime's own login, or an API key " +
    "stored masked), model catalog with prices, extra environment and concurrency limit.",
  runtimes:
    "The agent runtimes this version can run, as found on the server's PATH. A runtime's capabilities decide the " +
    "roles it can run. Runtimes are built in; a different binary comes from the server's PATH.",
  [AGENT_PROFILE.slug]: AGENT_PROFILE.help,
  roles:
    `What every role inherits, then each role: its ${AGENT_PROFILE.name}, harness, effort, environment and spend cap ` +
    "per run, its role prompt, timeout and concurrency. Each field shows what applies now and where it comes from.",
  catalog:
    "Plugins, skills and MCP servers: import, upload, take from git or write them, then install them globally or " +
    "in a project, for the roles that load them.",
  channels:
    "Connect external messaging platforms. Channels read credentials from environment variables at server " +
    "startup: restart the server after enabling a channel. Each one passes on only the senders it lists.",
  transfer:
    "One file with the role settings, the providers and Agent Profiles, the catalog with its files and MCP servers, " +
    "global installs and channels.",
};

/** One Settings page (#settings/<page>[/<sub>]); the sidebar's Settings group lists them. */
export function SettingsPage({ sub, onNavigate }: { sub: string; onNavigate: (sub: string) => void }) {
  const [first, ...rest] = sub.split("/");
  const page: SettingsPageId = isSettingsPage(first) ? first : DEFAULT_SETTINGS_PAGE;
  const inner = rest.join("/");

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-bold">{settingsPageLabel(page)}</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{DESCRIPTIONS[page]}</p>
      </div>
      {page === "providers" && <ProvidersSection adding={inner === "new"} />}
      {page === "runtimes" && <RuntimesSection />}
      {page === AGENT_PROFILE.slug && <AgentProfilesSection />}
      {page === "roles" && <RolesSection sub={inner} onNavigate={(role) => onNavigate(`roles/${role}`)} />}
      {page === "catalog" && <CatalogCard />}
      {page === "channels" && <ChannelsSection />}
      {page === "transfer" && <ConfigTransferCard />}
    </div>
  );
}
