<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <section v-if="meta.key === 'adhoc_export'" class="delivery-panel" data-testid="delivery-panel">
      <div class="delivery-head">
        <div>
          <h3>分区交付批</h3>
          <p class="page-desc">按任务各自关联的分区交付，同批只允许一个已核对口径；未齐备的分区不产出文件。</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-testid="delivery-create" type="button" @click="createBatch">
            登记交付批（已选 {{ selectedTaskIds.length }} 项）
          </button>
          <button class="btn" data-testid="delivery-repair" type="button" @click="repairNorth">
            修复北区输入
          </button>
          <button class="btn" data-testid="delivery-retry" type="button" :disabled="!batch" @click="retryBatch">
            补算仍需处理的分区
          </button>
          <button class="btn primary" data-testid="delivery-reconcile" type="button" :disabled="!batch" @click="reconcileBatch">
            逐条核对并汇总跨区总账
          </button>
        </div>
      </div>
      <p v-if="deliveryMessage" class="delivery-message" data-testid="delivery-message">{{ deliveryMessage }}</p>

      <div v-if="batch" class="delivery-grid">
        <article v-for="binding in batch.bindings" :key="binding.task_id" class="delivery-card">
          <strong>{{ binding.zone }}区 · 任务 {{ binding.task_id }}</strong>
          <span class="page-desc">{{ binding.task_name }}</span>
          <span class="page-desc">口径：{{ binding.caliber_version }}</span>
          <span class="page-desc">模板：{{ binding.template_name }}（#{{ binding.template_id }}）</span>
          <span class="page-desc">
            覆盖：{{ partitionOf(binding.zone)?.coverage?.included ?? 0 }}/{{ partitionOf(binding.zone)?.coverage?.expected ?? 0 }}
            <template v-if="partitionOf(binding.zone)?.missing_detail_ids?.length">
              · 缺 {{ partitionOf(binding.zone)?.missing_detail_ids?.join(',') }}
            </template>
          </span>
          <span class="page-desc">状态：{{ partitionOf(binding.zone)?.status ?? '未领取' }}</span>
          <span v-for="artifact in artifactsOf(binding.zone)" :key="artifact.artifact_id" class="page-desc">
            产物 v{{ artifact.version }} · {{ artifact.row_count }} 行 · {{ artifact.input_fingerprint }}
          </span>
        </article>
      </div>

      <div v-if="reconcileResult" class="delivery-reconcile" data-testid="delivery-reconcile-result">
        <p>{{ reconcileResult.message }}</p>
        <table class="data-table">
          <thead>
            <tr><th>核算区</th><th>产物版本</th><th>行数</th><th>GMV</th><th>订单数</th><th>核验</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="check in reconcileResult.checks" :key="check.zone">
              <td>{{ check.zone }}</td>
              <td>{{ check.artifact_version ?? '—' }}</td>
              <td>{{ check.row_count ?? '—' }}</td>
              <td>{{ check.totals?.GMV ?? '—' }}</td>
              <td>{{ check.totals?.订单数 ?? '—' }}</td>
              <td>{{ check.delivered ? (check.valid ? '可核验' : '不可核验') : '未交付' }}</td>
              <td>{{ check.reason }}</td>
            </tr>
            <tr>
              <td>跨区总计</td>
              <td>—</td>
              <td>{{ reconcileResult.cross_zone.combined_row_count }}</td>
              <td>{{ reconcileResult.cross_zone.combined_totals.GMV ?? '—' }}</td>
              <td>{{ reconcileResult.cross_zone.combined_totals.订单数 ?? '—' }}</td>
              <td>{{ reconcileResult.cross_zone.matches_upstream ? '与上游一致' : '与上游不一致' }}</td>
              <td>文件加总</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <input
              v-if="meta.key === 'adhoc_export'"
              type="checkbox"
              :data-testid="`select-task-${row.id}`"
              :checked="selectedTaskIds.includes(Number(row.id))"
              @change="toggleTask(Number(row.id))"
            />
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import {
  createDeliveryBatch,
  dispatchDeliveryBatch,
  reconcileDeliveryBatch,
  repairPartition,
  retryDeliveryBatch,
} from '@/api/delivery'
import type { DeliveryBatch, DeliveryReconcile } from '@/api/delivery'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const selectedTaskIds = ref<number[]>([])
const batch = ref<DeliveryBatch | null>(null)
const reconcileResult = ref<DeliveryReconcile | null>(null)
const deliveryMessage = ref('')

function toggleTask(taskId: number) {
  selectedTaskIds.value = selectedTaskIds.value.includes(taskId)
    ? selectedTaskIds.value.filter((id) => id !== taskId)
    : [...selectedTaskIds.value, taskId]
}

function partitionOf(zone: string) {
  return batch.value?.partitions.find((item) => item.zone === zone)
}

function artifactsOf(zone: string) {
  return (batch.value?.artifacts ?? []).filter((item) => item.zone === zone)
}

async function createBatch() {
  deliveryMessage.value = ''
  reconcileResult.value = null
  try {
    const result = await createDeliveryBatch(selectedTaskIds.value, '202610')
    if (!result.ok || !result.batch) throw new Error(result.message ?? '登记交付批失败')
    batch.value = result.batch
    const dispatched = await dispatchDeliveryBatch(result.batch.batch_id)
    const delivered = dispatched.delivered.map((item) => item.zone).join('、') || '无'
    const pending = dispatched.pending.map((item) => `${item.zone}（${item.reason}）`).join('、') || '无'
    deliveryMessage.value = `先领取已齐分区：${delivered}；待补算：${pending}`
  } catch (error) {
    deliveryMessage.value = error instanceof Error ? error.message : '登记交付批失败'
  }
}

async function repairNorth() {
  deliveryMessage.value = ''
  try {
    const result = await repairPartition('北区')
    deliveryMessage.value = result.restored_detail_ids.length
      ? `已补齐北区明细：${result.restored_detail_ids.join('、')}`
      : '北区没有缺失明细'
  } catch (error) {
    deliveryMessage.value = error instanceof Error ? error.message : '修复输入失败'
  }
}

async function retryBatch() {
  if (!batch.value) return
  deliveryMessage.value = ''
  try {
    const result = await retryDeliveryBatch(batch.value.batch_id)
    const recomputed = result.recomputed.map((item) => `${item.zone} v${item.artifact_version}`).join('、') || '无'
    const untouched = result.untouched.map((item) => item.zone).join('、') || '无'
    deliveryMessage.value = `补算：${recomputed}；已核验无需重跑：${untouched}`
  } catch (error) {
    deliveryMessage.value = error instanceof Error ? error.message : '补算失败'
  }
}

async function reconcileBatch() {
  if (!batch.value) return
  deliveryMessage.value = ''
  try {
    reconcileResult.value = await reconcileDeliveryBatch(batch.value.batch_id)
    deliveryMessage.value = reconcileResult.value.message
  } catch (error) {
    deliveryMessage.value = error instanceof Error ? error.message : '对账失败'
  }
}
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    if (meta.key === 'adhoc_export' && !selectedTaskIds.value.length) {
      // 默认勾选带核算区的分区任务，其余任务没有分区归属，登记交付批时会被拒绝。
      selectedTaskIds.value = rows.value
        .filter((row) => String(row['分区过滤'] ?? '').includes('核算区'))
        .map((row) => Number(row.id))
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
