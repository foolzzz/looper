import { useEffect, useState } from "react";
import { Activity, Bot, Cpu, ListTodo } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { fetchStatus, type SystemStatus } from "@/lib/api";

function StatCard({
  label,
  value,
  icon: Icon,
  variant = "default",
}: {
  label: string;
  value: string | number;
  icon: React.ElementType;
  variant?: "default" | "success" | "warning" | "destructive";
}) {
  const colorMap = {
    default: "text-primary",
    success: "text-success",
    warning: "text-warning",
    destructive: "text-destructive",
  };
  return (
    <div className="rounded-lg border border-border bg-card p-5">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{label}</p>
        <Icon className={`h-5 w-5 ${colorMap[variant]}`} />
      </div>
      <p className="mt-2 text-2xl font-bold">{value}</p>
    </div>
  );
}

export function DashboardPage() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let timer: ReturnType<typeof setInterval>;
    const load = () =>
      fetchStatus()
        .then((s) => {
          setStatus(s);
          setError("");
        })
        .catch((e) => setError(e.message));
    load();
    timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  if (error && !status) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <p className="text-lg text-destructive">Connection failed</p>
          <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          <p className="mt-3 text-xs text-muted-foreground">
            Make sure <code className="text-foreground">looper serve</code> is running
          </p>
        </div>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-muted-foreground">Connecting...</p>
      </div>
    );
  }

  const agents = Object.entries(status.agents || {});
  const healthyCount = agents.filter(([, a]) => a.healthy).length;
  const tasks = (status as any).tasks || { total: 0, running: 0, pending: 0, completed: 0 };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">System overview</p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="System Status"
          value={status.status === "running" ? "Running" : "Stopped"}
          icon={Activity}
          variant={status.status === "running" ? "success" : "destructive"}
        />
        <StatCard label="Agents" value={`${healthyCount}/${agents.length}`} icon={Bot} variant="default" />
        <StatCard label="Active Tasks" value={tasks.running} icon={Cpu} variant="warning" />
        <StatCard label="Total Tasks" value={tasks.total} icon={ListTodo} />
      </div>

      <div className="rounded-lg border border-border bg-card">
        <div className="border-b border-border px-5 py-3">
          <h2 className="text-sm font-semibold">Agents</h2>
        </div>
        <div className="divide-y divide-border">
          {agents.length === 0 ? (
            <p className="px-5 py-4 text-sm text-muted-foreground">No agents registered</p>
          ) : (
            agents.map(([id, info]) => (
              <div key={id} className="flex items-center justify-between px-5 py-3">
                <div className="flex items-center gap-3">
                  <div
                    className={`h-2 w-2 rounded-full ${info.healthy ? "bg-success" : "bg-destructive"}`}
                  />
                  <span className="text-sm font-medium">{id}</span>
                  <span className="text-xs text-muted-foreground">{(info as any).agent_type}</span>
                </div>
                <Badge variant={info.healthy ? "success" : "destructive"}>
                  {info.healthy ? "Healthy" : "Unhealthy"}
                </Badge>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
