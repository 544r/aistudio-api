import type { AccountRotationStats } from './rotation.ts'

export interface Account {
  id: string
  name: string
  email: string | null
  created_at: string
  last_used?: string | null
  auth_user: string
  cookie_id?: string | null
  tier?: string
  enabled: boolean
  group: string
  notes?: string
}

export type AccountWithStats = Omit<Account, 'last_used' | 'auth_user' | 'email'> &
  Partial<AccountRotationStats> & {
    auth_user: string
    email: string | null
    last_used?: string | null
  }

export interface AccountPreset {
  id: string
  name: string
  description?: string
  action: 'snapshot' | 'enable_all' | 'disable_all' | 'filter_tier' | 'filter_group' | string
  account_ids: string[]
  target_tiers: string[]
  target_groups: string[]
  is_system: boolean
  created_at?: string
  updated_at?: string
}

export interface CreatePresetRequest {
  name: string
  description?: string
  action?: string
  account_ids?: string[]
  target_tiers?: string[]
  target_groups?: string[]
}

export interface SnapshotPresetRequest {
  name: string
  description?: string
}

export interface AccountHealthCheckResponse {
  checked: number
  valid: number
  expired: string[]
  unknown: string[]
}

export interface DeleteExpiredAccountsResponse {
  deleted: number
  ids: string[]
  names: string[]
}

export interface DetectEmailResponse extends Account {
  detected_email: string | null
  email_updated: boolean
  email_conflict: boolean
}

export interface ApplyPresetResponse {
  ok: boolean
  preset_id: string
  preset_name: string
  enabled_count: number
  disabled_count: number
  enabled_account_ids: string[]
  disabled_account_ids: string[]
}

export interface ImportCookiesRequest {
  cookies: string
  name?: string
  email?: string
  account_id?: string
  auth_user?: string
  tier?: string
}

export interface ImportCookiesResponse {
  account_id: string
  name: string
  cookie_count: number
  domain_summary: Record<string, number>
  auth_user: string
  tier?: string
}

export interface ProbeAndImportRequest {
  cookies: string
  name_prefix?: string
}

export interface ProbeAndImportResponse {
  imported_count: number
  accounts: Account[]
}

export interface BundleAccountItem {
  id?: string
  name?: string
  email?: string
  cookies: string
  auth_user?: string
  tier?: string
}

export interface ImportBundleRequest {
  file_path?: string
  content?: string
  accounts?: BundleAccountItem[]
}

export interface ImportBundleResponse {
  imported_count: number
  accounts: Account[]
}

export interface CookiePushChange {
  name: string
  domain: string
  path: string
  category: 'core' | 'rolling' | 'other'
  kind: 'changed' | 'added' | 'removed'
  before: string | null
  after: string | null
}

export interface CookiePushEvent {
  seq: number
  at: number
  account_id: string
  name: string
  email: string | null
  auth_user: string
  source: string | null
  received_count: number
  cookie_count: number
  fingerprint: string
  merged_fingerprint: string
  previous_fingerprint: string | null
  first: boolean
  changed: boolean
  merged_changed: boolean
  changes: CookiePushChange[]
  missing_advisory: string[]
  merged_missing_advisory: string[]
  session_valid: boolean | null
  injected: boolean
  created: boolean
  updated_accounts: number
}

export interface CookiePushHistoryResponse {
  entries: CookiePushEvent[]
  total: number
  capacity: number
}
