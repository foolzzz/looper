import { callV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

/** JSON of the leads and intents operations. Shapes are the frozen contracts of those operations. */

async function call<T>(path: string, body: unknown = {}): Promise<T> {
  const result = await callV1<T>(path, body);
  if (isConfirmation(result)) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

function isConfirmation(value: unknown): value is V1PendingConfirmation {
  return Boolean(value) && typeof value === "object" && (value as { status?: string }).status === "needs_confirmation";
}

export interface LeadSource {
  chat: string;
  chat_id: string;
  chat_kind: string;
  sender: string;
  sender_id: string;
  sent_at: string | null;
  message_id: string;
  link: string | null;
}

export interface Lead {
  id: string;
  kind: string;
  summary: string;
  detail: string;
  confidence: string;
  state: string;
  valid_until: string | null;
  superseded_by: string | null;
  sources: LeadSource[];
  sources_total: number;
}

export interface LeadsSearch {
  notice: string;
  leads: Lead[];
  /** How many matching leads are past this page. Zero is the last page, and must not be rendered as text. */
  more: number;
}

export interface LeadSubscription {
  schedule_id: string;
  name: string;
  enabled: boolean;
  project_id: string | null;
}

export interface LeadsSettings {
  enabled: boolean;
  valid_days: number;
  subscriptions: LeadSubscription[];
}

export interface LeadChat {
  chat_id: string;
  name: string;
  kind: string;
  extraction: "included" | "excluded";
  extraction_excluded_at: string | null;
  last_message_at: string | null;
  leads: number;
  open_intents: number;
}

export interface LeadsChats {
  source_id: string | null;
  chats: LeadChat[];
  total: number;
}

export interface Named {
  id: string;
  name: string;
}

/** A workflow on an intent. The frozen view uses `key`, unlike a project which uses `id`. */
export interface IntentWorkflowRef {
  key: string;
  name: string;
}

export interface IntentWorkflow extends IntentWorkflowRef {
  source: "intent" | "type_project" | "type" | "project";
}

export interface Intent {
  id: string;
  type: { key: string; name: string };
  type_version: number;
  title: string;
  summary: string;
  confidence: string;
  state: string;
  flagged: boolean;
  chat: { chat_id: string; name: string; kind: string };
  project: Named | null;
  workflow: IntentWorkflowRef | null;
  effective_workflow: IntentWorkflow | null;
  candidates: Named[];
  merged_into: string | null;
  task: { id: string; title: string; status: string } | null;
  sources: LeadSource[];
  sources_total: number;
  push_state: "none" | "pending" | "sending" | "sent" | "failed" | "uncertain";
  expires_at: string | null;
  handled_at: string | null;
  handled_via: string | null;
  reject_reason: string | null;
  created_at: string;
}

export interface IntentTypeProject {
  project_id: string;
  project_name: string;
  workflow_key: string | null;
}

export interface IntentType {
  key: string;
  builtin: boolean;
  enabled: boolean;
  archived: boolean;
  version: number;
  name: string;
  guidance: string;
  priority: "urgent" | "normal" | "quiet";
  default_workflow: string | null;
  recommended: boolean;
  projects: IntentTypeProject[];
}

export interface IntentTypeVersion {
  version: number;
  name: string;
  guidance: string;
  priority: string;
  default_workflow: string | null;
  rolled_back_from: number | null;
  edited_via: string;
  created_at: string;
}

export interface IntentStatsType {
  key: string;
  name: string;
  recognized: number;
  tasked: number;
  rejected: number;
  expired: number;
  merged: number;
  /** Dropped by the second model's check: never shown, so counted in neither rate (absent from an older server). */
  dropped?: number;
  adoption_rate: number | null;
  rejection_rate: number | null;
}

export interface IntentSubscription extends LeadSubscription {
  target_ok: boolean;
}

export interface IntentsSettings {
  enabled: boolean;
  subscriptions: IntentSubscription[];
}

export interface IntentBinding {
  chat_id: string;
  chat_name: string;
  chat_kind: string;
  project: Named;
}

export interface AlertSender {
  app_id: string;
  added_at?: string;
  chats: string[];
  messages_30d: number;
}

export interface FollowedGroup {
  chat_id: string;
  name: string;
  kind: string;
  added_at: string;
  read_from: string;
  readable: boolean;
  read_today: number;
  skipped_today: number;
  messages_24h: number;
  intents_7d: number;
}

export interface FollowedGroups {
  enabled: boolean;
  delivery_enabled: boolean;
  run_max: number;
  day_max: number;
  max_groups: number;
  groups: FollowedGroup[];
}

export interface FollowedGroupPreview {
  group: { chat_id: string; name: string; kind: string; messages_24h: number; estimate_usd: [number, number] };
}

export interface IntentRecognitionPreview {
  chat: { chat_id: string; name: string };
  range_from: string;
  range_to: string;
  effective_from: string;
  events: number;
  truncated: "messages" | "budget" | null;
  estimate_usd: [number, number];
  schedule_id: string;
}

export interface IntentRecognition {
  id: string;
  chat_id: string;
  name: string;
  range_from: string;
  range_to: string;
  state: "running" | "done" | "failed";
  events_in_range: number;
  events_read: number;
  truncated: "messages" | "budget" | null;
  cost_usd: number;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  intents: { id: string; type: { key: string; name: string }; title: string; state: string }[];
}

export interface IntentRecognitions {
  enabled: boolean;
  recognitions: IntentRecognition[];
}

export interface IntentRecognitionRange {
  source_id?: string;
  chat_id: string;
  since?: string;
  until?: string;
}

export interface SubscriptionPresetFields {
  kind: "subscription";
  name: string;
  cron: string;
  timezone: string;
  requirement: string;
  run_budget_usd: number;
  daily_budget_usd: number | null;
  notify: "failures" | "alerts" | "always" | "never";
  subscription: {
    source_id: string;
    chat_kinds: ("p2p" | "group" | "topic" | "unknown")[];
    sender_kinds: ("user" | "app")[];
    include_history: boolean;
    max_age_minutes: number;
    max_events: number;
    text_max_chars: number;
    quiet_word: string;
    output_header: string;
    output_kind: "leads" | "intents" | "triage";
    exclude_mentions_me?: boolean;
  };
}

export function leadsSearch(args: {
  query?: string; chat?: string; person?: string; since?: string; days?: number; kind?: string; state?: string; limit?: number;
} = {}): Promise<LeadsSearch> {
  return call("/api/v1/leads/search", args);
}

export function leadsMark(args: { lead_id: string; note?: string }): Promise<{ lead: Lead }> {
  return call("/api/v1/leads/mark", args);
}

export function leadsDelete(args: { lead_id: string }): Promise<{ deleted: string }> {
  return call("/api/v1/leads/delete", args);
}

export function leadsSettingsGet(): Promise<LeadsSettings> {
  return call("/api/v1/leads/settings_get");
}

export function leadsSettingsPut(args: { enabled?: boolean; valid_days?: number }): Promise<LeadsSettings> {
  return call("/api/v1/leads/settings_put", args);
}

export function leadsChats(args: {
  source_id?: string; query?: string; extraction?: "included" | "excluded"; limit?: number; offset?: number;
} = {}): Promise<LeadsChats> {
  return call("/api/v1/leads/chats", args);
}

export function leadsSetChat(args: {
  source_id?: string; chat_id: string; extraction: "included" | "excluded"; delete_extracted?: boolean;
}): Promise<{
  chat_id: string; extraction: string; deleted_leads: number; deleted_intents: number;
  /** Intents from other chats that had been merged into deleted ones, put back to pending. Absent on older answers. */
  detached_intents?: number;
}> {
  return call("/api/v1/leads/set_chat", args);
}

export function subscriptionPreset(args: { output_kind: "leads" | "intents" | "triage" }): Promise<{ fields: SubscriptionPresetFields }> {
  return call("/api/v1/schedule_admin/subscription_preset", args);
}

export function intentsList(args: {
  state?: string[]; type?: string; project_id?: string; chat?: string; limit?: number; before_id?: string;
} = {}): Promise<{ intents: Intent[]; more: boolean }> {
  return call("/api/v1/intents/list", args);
}

export function intentsGet(args: { intent_id: string }): Promise<{
  intent: Intent;
  posts: { kind: string; state: string; created_at: string; edited_at: string | null }[];
  merged: { id: string; summary: string; created_at: string }[];
}> {
  return call("/api/v1/intents/get", args);
}

export function intentsUpdate(args: { intent_id: string; project_id?: string | null; workflow_key?: string | null }): Promise<{ intent: Intent }> {
  return call("/api/v1/intents/update", args);
}

export function intentsBuild(args: { intent_id: string; project_id?: string; workflow_key?: string }): Promise<{ intent: Intent; task_id: string }> {
  return call("/api/v1/intents/build", args);
}

export function intentsReject(args: { intent_id: string; reason?: string }): Promise<{ intent: Intent }> {
  return call("/api/v1/intents/reject", args);
}

export function intentsStats(args: { days?: number } = {}): Promise<{ days: number; types: IntentStatsType[] }> {
  return call("/api/v1/intents/stats", args);
}

export function intentsSettingsGet(): Promise<IntentsSettings> {
  return call("/api/v1/intents/settings_get");
}

export function intentsSettingsPut(args: { enabled: boolean }): Promise<IntentsSettings> {
  return call("/api/v1/intents/settings_put", args);
}

export function intentTypesList(): Promise<{ types: IntentType[] }> {
  return call("/api/v1/intent_types/list");
}

export function intentTypesHistory(args: { key: string }): Promise<{ key: string; current: number; versions: IntentTypeVersion[] }> {
  return call("/api/v1/intent_types/history", args);
}

export function intentTypesCreate(args: {
  key: string; name: string; guidance: string; priority: string; default_workflow?: string | null;
}): Promise<{ type: IntentType }> {
  return call("/api/v1/intent_types/create", args);
}

export function intentTypesSave(args: {
  key: string; name?: string; guidance?: string; priority?: string; default_workflow?: string | null; enabled?: boolean;
}): Promise<{ type: IntentType }> {
  return call("/api/v1/intent_types/save", args);
}

export function intentTypesRollback(args: { key: string; version: number }): Promise<{ type: IntentType }> {
  return call("/api/v1/intent_types/rollback", args);
}

export function intentTypesArchive(args: { key: string }): Promise<{ type: IntentType }> {
  return call("/api/v1/intent_types/archive", args);
}

export function intentTypesSetProjectTemplate(args: {
  key: string; project_id: string; workflow_key: string | null;
}): Promise<{ type: IntentType }> {
  return call("/api/v1/intent_types/set_project_template", args);
}

export function intentBindingsList(args: { source_id?: string; query?: string } = {}): Promise<{ bindings: IntentBinding[] }> {
  return call("/api/v1/intent_bindings/list", args);
}

export function intentBindingsPut(args: { chat_id: string; project_id: string }): Promise<{ bindings: IntentBinding[] }> {
  return call("/api/v1/intent_bindings/put", args);
}

export function intentBindingsDelete(args: { chat_id: string }): Promise<{ bindings: IntentBinding[] }> {
  return call("/api/v1/intent_bindings/delete", args);
}

export function intentAlertSendersList(args: { source_id?: string } = {}): Promise<{ senders: AlertSender[]; candidates: AlertSender[] }> {
  return call("/api/v1/intent_alert_senders/list", args);
}

export function intentAlertSendersPut(args: { app_id: string }): Promise<{ senders: AlertSender[] }> {
  return call("/api/v1/intent_alert_senders/put", args);
}

export function intentAlertSendersDelete(args: { app_id: string }): Promise<{ senders: AlertSender[] }> {
  return call("/api/v1/intent_alert_senders/delete", args);
}

export function intentGroupReadsList(args: { source_id?: string } = {}): Promise<FollowedGroups> {
  return call("/api/v1/intent_group_reads/list", args);
}

export function intentGroupReadsPreview(args: { source_id?: string; chat_id: string }): Promise<FollowedGroupPreview> {
  return call("/api/v1/intent_group_reads/preview", args);
}

export function intentGroupReadsPut(args: { source_id?: string; chat_id: string }): Promise<FollowedGroups> {
  return call("/api/v1/intent_group_reads/put", args);
}

export function intentGroupReadsDelete(args: { source_id?: string; chat_id: string }): Promise<FollowedGroups> {
  return call("/api/v1/intent_group_reads/delete", args);
}

export function intentGroupReadsSettingsPut(args: { source_id?: string; enabled: boolean }): Promise<FollowedGroups> {
  return call("/api/v1/intent_group_reads/settings_put", args);
}

export function intentRecognitionsPreview(args: IntentRecognitionRange): Promise<IntentRecognitionPreview> {
  return call("/api/v1/intent_recognitions/preview", args);
}

export function intentRecognitionsStart(args: IntentRecognitionRange): Promise<{ recognition: IntentRecognition }> {
  return call("/api/v1/intent_recognitions/start", args);
}

export function intentRecognitionsList(args: { source_id?: string; limit?: number } = {}): Promise<IntentRecognitions> {
  return call("/api/v1/intent_recognitions/list", args);
}

export function intentRecognitionsSettingsPut(args: { enabled: boolean }): Promise<IntentRecognitions> {
  return call("/api/v1/intent_recognitions/settings_put", args);
}
