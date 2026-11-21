/** 即席导出的分区交付：登记批次、领取、重试和对账。 */
import { request } from './client'

export type DeliveryBinding = {
  task_id: number
  task_name: string
  zone: string
  month: string
  cube_name: string
  template_id: number
  template_name: string
  caliber_version: string
}

export type DeliveryPartition = {
  zone: string
  task_id: number
  status: string
  reason?: string
  coverage?: { expected: number; included: number; missing: number; ratio: number }
  missing_detail_ids?: string[]
}

export type DeliveryArtifact = {
  artifact_id: number
  task_id: number
  zone: string
  version: number
  caliber_version: string
  input_fingerprint: string
  row_count: number
  file_path: string
  checksum: string
  status: string
}

export type DeliveryBatch = {
  batch_id: number
  batch_name: string
  month: string
  caliber_version: string
  caliber_desc: string
  status: string
  task_ids: number[]
  bindings: DeliveryBinding[]
  partitions: DeliveryPartition[]
  artifacts: DeliveryArtifact[]
}

export type DeliveryCheck = {
  zone: string
  delivered: boolean
  valid?: boolean
  artifact_version?: number
  row_count?: number
  totals?: Record<string, number>
  fingerprint_ok?: boolean
  checksum_ok?: boolean
  reason: string
}

export type DeliveryReconcile = {
  ok: boolean
  batch_id: number
  message: string
  checks: DeliveryCheck[]
  cross_zone: {
    zones: Record<string, Record<string, number>>
    combined_row_count: number
    combined_totals: Record<string, number>
    upstream_totals: Record<string, number>
    matches_upstream: boolean
  }
  pending_zones: string[]
}

export type DeliveryDispatch = {
  ok: boolean
  delivered: { zone: string; artifact_version: number; row_count: number }[]
  pending: { zone: string; reason: string }[]
  fully_delivered: boolean
  scan_count: number
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return (await response.json()) as T
}

export async function createDeliveryBatch(
  taskIds: number[],
  month: string,
): Promise<{ ok: boolean; message?: string; batch?: DeliveryBatch }> {
  return post('/api/analytics/delivery/batches', { task_ids: taskIds, month })
}

export async function dispatchDeliveryBatch(batchId: number): Promise<DeliveryDispatch> {
  return post(`/api/analytics/delivery/batches/${batchId}/dispatch`, {})
}

export async function retryDeliveryBatch(batchId: number): Promise<{
  ok: boolean
  recomputed: { zone: string; artifact_version: number; row_count: number }[]
  untouched: { zone: string; reason: string }[]
  fully_delivered: boolean
}> {
  return post(`/api/analytics/delivery/batches/${batchId}/retry`)
}

export async function reconcileDeliveryBatch(batchId: number): Promise<DeliveryReconcile> {
  return post(`/api/analytics/delivery/batches/${batchId}/reconcile`)
}

export async function getDeliveryBatch(batchId: number): Promise<{ ok: boolean; batch: DeliveryBatch }> {
  const response = await request(`/api/analytics/delivery/batches/${batchId}`)
  return (await response.json()) as { ok: boolean; batch: DeliveryBatch }
}

export async function repairPartition(zone: string): Promise<{ ok: boolean; restored_detail_ids: string[] }> {
  return post('/api/analytics/delivery/partitions/repair', { zone })
}
