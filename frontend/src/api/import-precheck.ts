/**
 * 批次导入「入库分片预检」专用接口。
 * 预检类接口只读，不会写门店目标表；只有确认入库才会落库。
 */
import { request } from './client'

export type PrecheckItem = {
  row_key: string
  source_ref: string
  shard_index: number
  row_no: number
  rule_id: string
  rule_version: string
  store_code_field: string
  store_code_value: string
  mapped_record: Record<string, string>
  accepted: boolean
  reject_reason: string
}

export type PrecheckShardSummary = {
  shard_index: number
  status: string
  rule_version: string | null
  processed: number
  shard_size: number
  accepted: number
  rejected: number
  attempts: number
}

export type PrecheckChecklist = {
  ok: boolean
  message?: string
  batch_id: string
  precheck_status: string
  total_rows: number
  accepted: number
  rejected: number
  rule_versions: string[]
  version_drift: boolean
  confirmable: boolean
  blockers: string[]
  shards: PrecheckShardSummary[]
  items: PrecheckItem[]
}

type JsonResult = Record<string, unknown>

async function postJson(path: string, payload: Record<string, unknown>): Promise<JsonResult> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('接口请求失败')
  return (await response.json()) as JsonResult
}

export function splitBatch(
  batchId: number,
  shardSize: number,
  rawRows: Record<string, unknown>[],
): Promise<JsonResult> {
  return postJson(`/api/import_batch/${batchId}/split`, {
    shard_size: shardSize,
    raw_rows: rawRows,
  })
}

export function precheckShard(
  batchId: number,
  shardIndex: number,
  options: { ruleId?: number; failAfter?: number } = {},
): Promise<JsonResult> {
  return postJson(`/api/import_batch/${batchId}/precheck`, {
    shard_index: shardIndex,
    rule_id: options.ruleId ?? null,
    fail_after: options.failAfter ?? null,
  })
}

export function recheckShard(
  batchId: number,
  shardIndex: number,
  ruleId?: number,
): Promise<JsonResult> {
  return postJson(`/api/import_batch/${batchId}/recheck`, {
    shard_index: shardIndex,
    rule_id: ruleId ?? null,
  })
}

export async function fetchPrecheckChecklist(batchId: number): Promise<PrecheckChecklist> {
  const response = await request(`/api/import_batch/${batchId}/precheck/checklist`)
  if (!response.ok) throw new Error('预检清单读取失败')
  return (await response.json()) as PrecheckChecklist
}

export function confirmImport(batchId: number): Promise<JsonResult> {
  return postJson(`/api/import_batch/${batchId}/confirm`, {})
}

export async function downloadPrecheckReport(batchId: number): Promise<void> {
  const response = await request(`/api/import_batch/${batchId}/precheck/export`)
  if (!response.ok) throw new Error('导入预检文件下载失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `precheck_${batchId}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}
