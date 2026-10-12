import { useCallback, useEffect, useRef, useState } from "react";
import { Send, Loader2, Bot, User, Plus, ChevronDown } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { sendChat, saveChatMessage, fetchChatMessages, streamSSE, fetchAgentConfigs, type SSEEvent, type AgentConfig } from "@/lib/api";
import { cn } from "@/lib/utils";

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: number;
  streaming?: boolean;
}

const STORAGE_SESSION_KEY = "loopx-chat-session-id";

function getOrCreateSessionId(): string {
  const existing = localStorage.getItem(STORAGE_SESSION_KEY);
  if (existing) return existing;
  const id = `session-${crypto.randomUUID().slice(0, 8)}`;
  localStorage.setItem(STORAGE_SESSION_KEY, id);
  return id;
}

function loadMessages(sessionId: string): ChatMessage[] {
  try {
    const raw = localStorage.getItem(`loopx-chat-messages-${sessionId}`);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((m: ChatMessage) => ({ ...m, streaming: false }));
  } catch {
    return [];
  }
}

function saveMessages(sessionId: string, messages: ChatMessage[]) {
  const toSave = messages.map(({ streaming, ...rest }) => rest);
  localStorage.setItem(`loopx-chat-messages-${sessionId}`, JSON.stringify(toSave));
}

export function ChatPage() {
  const [sessionId, setSessionId] = useState(getOrCreateSessionId);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [phase, setPhase] = useState("");
  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [selectedAgent, setSelectedAgent] = useState("manager");
  const [agentDropdown, setAgentDropdown] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const dbLoadedRef = useRef(false);

  useEffect(() => {
    fetchAgentConfigs().then(setAgents).catch(() => {});
  }, []);

  const scrollToBottom = useCallback(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, []);

  useEffect(scrollToBottom, [messages, scrollToBottom]);

  useEffect(() => {
    if (!sending) inputRef.current?.focus();
  }, [sending]);

  // Load messages from DB on mount (or session change)
  useEffect(() => {
    dbLoadedRef.current = false;
    fetchChatMessages(sessionId).then((dbMsgs) => {
      if (dbMsgs.length > 0) {
        const loaded: ChatMessage[] = dbMsgs.map((m) => ({
          id: m.message_id,
          role: m.role as "user" | "assistant" | "system",
          content: m.content,
          timestamp: new Date(m.created_at).getTime(),
          streaming: false,
        }));
        setMessages(loaded);
      } else {
        const local = loadMessages(sessionId);
        if (local.length > 0) setMessages(local);
      }
      dbLoadedRef.current = true;
    }).catch(() => {
      const local = loadMessages(sessionId);
      if (local.length > 0) setMessages(local);
      dbLoadedRef.current = true;
    });
  }, [sessionId]);

  // Keep localStorage as cache
  useEffect(() => {
    if (messages.length > 0) {
      saveMessages(sessionId, messages);
    }
  }, [messages, sessionId]);

  const handleNewChat = () => {
    cleanupRef.current?.();
    cleanupRef.current = null;
    const newId = `session-${crypto.randomUUID().slice(0, 8)}`;
    localStorage.setItem(STORAGE_SESSION_KEY, newId);
    setSessionId(newId);
    setMessages([]);
    setSending(false);
    setPhase("");
    setInput("");
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;

    setInput("");
    setSending(true);
    setPhase("");

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      timestamp: Date.now(),
    };
    setMessages((prev) => [...prev, userMsg]);

    const assistantId = crypto.randomUUID();
    const assistantMsg: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      timestamp: Date.now(),
      streaming: true,
    };
    setMessages((prev) => [...prev, assistantMsg]);

    try {
      const { events_url } = await sendChat(text, sessionId, userMsg.id);

      cleanupRef.current = streamSSE(
        events_url,
        (event: SSEEvent) => {
          switch (event.kind) {
            case "answer.delta":
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? { ...m, content: m.content + ((event.payload as any).text || "") }
                    : m
                )
              );
              break;
            case "answer.final": {
              const finalText = (event.payload as any).text || "";
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? { ...m, content: finalText || m.content || "Done.", streaming: false }
                    : m
                )
              );
              saveChatMessage({
                session_id: sessionIdRef.current,
                message_id: assistantId,
                role: "assistant",
                content: finalText,
              });
              setSending(false);
              setPhase("");
              break;
            }
            case "agent.phase":
              setPhase((event.payload as any).label || "");
              break;
            case "turn.completed":
            case "turn.failed":
              setMessages((prev) => {
                const msg = prev.find((m) => m.id === assistantId);
                if (msg && msg.content) {
                  saveChatMessage({
                    session_id: sessionIdRef.current,
                    message_id: assistantId,
                    role: "assistant",
                    content: msg.content,
                  });
                }
                return prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m));
              });
              setSending(false);
              setPhase("");
              break;
          }
        },
        () => {
          setSending(false);
          setPhase("");
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m))
          );
        }
      );
    } catch {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? { ...m, content: "Failed to connect. Is the server running?", streaming: false }
            : m
        )
      );
      setSending(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-5 py-3">
        <div className="flex items-center gap-2">
          {/* Agent selector */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setAgentDropdown(!agentDropdown)}
              className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-accent transition-colors"
            >
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/10">
                <Bot className="h-3.5 w-3.5 text-primary" />
              </div>
              <div className="text-left">
                <p className="text-sm font-semibold leading-tight">
                  {agents.find((a) => a.agent_id === selectedAgent)?.display_name || selectedAgent}
                </p>
                <p className="text-[10px] text-muted-foreground leading-tight">
                  {agents.find((a) => a.agent_id === selectedAgent)?.role || "agent"} · {
                    agents.find((a) => a.agent_id === selectedAgent)?.runtime || "claude-code"
                  }
                </p>
              </div>
              <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
            </button>
            {agentDropdown && (
              <div className="absolute left-0 top-full z-50 mt-1 w-56 rounded-lg border border-border bg-popover shadow-lg">
                {agents.filter((a) => a.enabled).map((agent) => (
                  <button
                    key={agent.agent_id}
                    type="button"
                    onClick={() => {
                      setSelectedAgent(agent.agent_id);
                      setAgentDropdown(false);
                    }}
                    className={cn(
                      "flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent transition-colors first:rounded-t-lg last:rounded-b-lg",
                      agent.agent_id === selectedAgent && "bg-accent/50",
                    )}
                  >
                    <Bot className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                    <div className="min-w-0">
                      <p className="font-medium truncate">{agent.display_name || agent.agent_id}</p>
                      <p className="text-[10px] text-muted-foreground">{agent.role} · {agent.runtime}</p>
                    </div>
                    {agent.agent_id === selectedAgent && (
                      <span className="ml-auto text-primary text-xs">✓</span>
                    )}
                  </button>
                ))}
                {agents.length === 0 && (
                  <p className="px-3 py-2 text-xs text-muted-foreground">No agents</p>
                )}
              </div>
            )}
          </div>

          <div className="ml-auto flex items-center gap-3">
            <span className="font-mono text-[11px] text-muted-foreground">{sessionId}</span>
            <button
              type="button"
              onClick={handleNewChat}
              className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
              title="New conversation"
            >
              <Plus className="h-3.5 w-3.5" />
            </button>
            <div className="flex items-center gap-1.5">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-success" />
              </span>
              <span className="text-xs text-muted-foreground">Online</span>
            </div>
          </div>
        </div>
        {phase && (
          <p className="text-xs text-muted-foreground mt-1.5 flex items-center gap-1">
            <Loader2 className="h-3 w-3 animate-spin" />
            {phase}
          </p>
        )}
      </div>

      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-5 py-4 space-y-4"
        onClick={() => agentDropdown && setAgentDropdown(false)}
      >
        {messages.length === 0 && (
          <div className="flex h-full items-center justify-center">
            <div className="text-center">
              <Bot className="mx-auto h-12 w-12 text-muted-foreground/50" />
              <p className="mt-3 text-sm text-muted-foreground">Send a message to start</p>
            </div>
          </div>
        )}

        {messages.map((msg) => (
          <div
            key={msg.id}
            className={cn("flex gap-3", msg.role === "user" ? "justify-end" : "justify-start")}
          >
            {msg.role === "assistant" && (
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10">
                <Bot className="h-4 w-4 text-primary" />
              </div>
            )}
            <div
              className={cn(
                "max-w-[70%] rounded-lg px-4 py-2.5 text-sm",
                msg.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-foreground"
              )}
            >
              {msg.role === "assistant" ? (
                <div className="prose prose-invert prose-sm max-w-none [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
                  <ReactMarkdown>{msg.content || (msg.streaming ? "..." : "")}</ReactMarkdown>
                </div>
              ) : (
                <p className="whitespace-pre-wrap">{msg.content}</p>
              )}
              {msg.streaming && (
                <Loader2 className="mt-1.5 h-3 w-3 animate-spin text-muted-foreground" />
              )}
            </div>
            {msg.role === "user" && (
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent">
                <User className="h-4 w-4 text-muted-foreground" />
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="border-t border-border p-4">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleSend();
          }}
          className="flex items-center gap-2"
        >
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Type a message..."
            className="flex-1 rounded-lg border border-input bg-background px-4 py-2.5 text-sm text-foreground placeholder:text-muted-foreground outline-none focus:ring-2 focus:ring-ring"
            autoFocus
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
          >
            {sending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Send className="h-4 w-4" />
            )}
          </button>
        </form>
      </div>
    </div>
  );
}
