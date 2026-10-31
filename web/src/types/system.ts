export interface SystemConfig {
  port: number
  browser_port: number
  browser_headless: boolean
  proxy_configured: boolean
  auth_enabled: boolean
  timeout_stream: number
  timeout_replay: number
  yaml_content: string
  log_level?: string
  dump_requests?: boolean
  debug_env_active?: boolean
  dump_to_file?: boolean
  dump_dir?: string
}

export interface UpdateTimeoutsRequest {
  stream?: number
  replay?: number
}

export interface UpdateTimeoutsResponse {
  ok: boolean
  persisted: boolean
  timeout_stream: number
  timeout_replay: number
}
export interface UpdateLoggingConfigRequest {
  level?: string
  dump_requests?: boolean
  dump_to_file?: boolean
  dump_dir?: string
}
export interface UpdateLoggingConfigResponse {
  ok: boolean
  log_level: string
  dump_requests: boolean
  debug_env_active: boolean
  dump_to_file: boolean
  dump_dir: string
}

export interface HealthCheckResponse {
  status: string
  busy: boolean
}
export interface ApiKeyItem {
  name: string
  key: string
  created_at?: string | null
}

export interface CreateApiKeyRequest {
  name?: string
  key?: string
}

export interface UpdateApiKeyRequest {
  name: string
}

export interface LogEntry {
  seq: number
  time: string
  level: string
  logger: string
  message: string
}

export interface LogsResponse {
  entries: LogEntry[]
  last_seq: number
}

export interface DumpFileInfo {
  name: string
  size: number
  modified: string
}

export interface DumpFilesResponse {
  dir: string
  files: DumpFileInfo[]
}
