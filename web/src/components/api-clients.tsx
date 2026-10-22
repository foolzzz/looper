import { useCallback, useEffect, useId, useState } from "react";
import { Check, Copy, Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LABEL, RegistryList, Row, RowName } from "@/components/registry-ui";
import { createApiClient, fetchApiClients, revokeApiClient, type ApiClient } from "@/lib/api";
import { errorText, useAction } from "@/lib/use-action";

const date = (iso: string) => new Date(iso).toLocaleDateString();

/** A value to copy, with its Copy button. */
function CopyButton({ value, what }: { value: string; what: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Button type="button" size="sm" variant="outline" onClick={copy} aria-label={`Copy ${what}`}>
      {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      {copied ? "Copied" : "Copy"}
    </Button>
  );
}

/** The new client's token, shown once; it lives in this component's state and nowhere else. */
function TokenBox({ token, onDone }: { token: string; onDone: () => void }) {
  const id = useId();
  const login = `loopercli login --url ${window.location.origin}`;
  return (
    <div className="space-y-3 rounded-lg border border-border bg-card px-5 py-3">
      <div className="space-y-1">
        <label htmlFor={`${id}-token`} className={LABEL}>Token</label>
        <div className="flex gap-2">
          <Input id={`${id}-token`} readOnly value={token} spellCheck={false} className="font-mono"
                 onFocus={(e) => e.currentTarget.select()} />
          <CopyButton value={token} what="token" />
        </div>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${id}-login`} className={LABEL}>Sign loopercli in</label>
        <div className="flex gap-2">
          <code id={`${id}-login`} className="min-w-0 flex-1 break-all rounded-md bg-muted px-3 py-2 font-mono text-xs">
            {login}
          </code>
          <CopyButton value={login} what="command" />
        </div>
      </div>
      <p className="text-xs text-muted-foreground">Paste the token when loopercli asks for it. It is not shown again.</p>
      <Button type="button" size="sm" onClick={onDone}>Done</Button>
    </div>
  );
}

function NewClientForm({ reload, onToken }: { reload: () => Promise<void>; onToken: (token: string) => void }) {
  const id = useId();
  const [name, setName] = useState("");
  const action = useAction();

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    let token = "";
    const ok = await action.run(async () => {
      token = (await createApiClient(name.trim())).token;
    });
    if (!ok) return;
    setName("");
    onToken(token);
    await reload();
  };

  return (
    <form onSubmit={create} className="space-y-2">
      <div className="space-y-1">
        <label htmlFor={`${id}-name`} className={LABEL}>Name *</label>
        <div className="flex gap-2">
          <Input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} maxLength={40}
                 autoComplete="off" aria-invalid={!!action.error} />
          <Button type="submit" size="sm" disabled={action.busy || !name.trim()}>
            {action.busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            Create
          </Button>
        </div>
        <p className="text-[11px] text-muted-foreground">
          What this client is called in the task and Manager trails, e.g. the machine or script that uses it.
        </p>
      </div>
      {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
    </form>
  );
}

function ClientRow({ client, reload }: { client: ApiClient; reload: () => Promise<void> }) {
  const action = useAction();
  const revoke = async () => {
    if (!confirm(`Revoke ${client.name}? The token stops working at once; what it did stays recorded under its name.`)) return;
    if (await action.run(() => revokeApiClient(client.id))) await reload();
  };
  return (
    <Row
      title={<RowName name={client.name} builtin={false} />}
      actions={
        <Button type="button" variant="ghost" size="sm" onClick={revoke} disabled={action.busy}
                aria-label={`Revoke API client ${client.name}`}
                className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive">
          <Trash2 className="h-3.5 w-3.5" /> Revoke
        </Button>
      }
      below={action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
    >
      <p className="text-xs text-muted-foreground">
        created {date(client.created_at)} · {client.last_used_at ? `last used ${date(client.last_used_at)}` : "never used"}
      </p>
    </Row>
  );
}

/** Settings → API clients: the tokens of agents and scripts that use Looper through loopercli. */
export function ApiClientsSection() {
  const [clients, setClients] = useState<ApiClient[] | null>(null);
  const [error, setError] = useState("");
  const [token, setToken] = useState("");

  const load = useCallback(async () => {
    try {
      setClients(await fetchApiClients());
      setError("");
    } catch (err) {
      setError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!clients) {
    return error ? (
      <div className="space-y-3">
        <p role="alert" className="break-words text-sm text-destructive">{error}</p>
        <Button variant="outline" onClick={() => void load()}>Retry</Button>
      </div>
    ) : (
      <div className="flex h-32 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {token && <TokenBox token={token} onDone={() => setToken("")} />}
      <RegistryList
        title="API clients"
        hint="One token per agent or script; the list never shows a token."
        addLabel="New client"
        defaultAdding
        form={() => <NewClientForm reload={load} onToken={setToken} />}
      >
        {clients.map((c) => <ClientRow key={c.id} client={c} reload={load} />)}
      </RegistryList>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    </div>
  );
}
