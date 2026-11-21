<template>
  <section class="precheck-panel" data-testid="precheck-panel">
    <header class="precheck-head">
      <h3>入库分片预检</h3>
      <p class="page-desc">
        门店数据切分后逐片预检，确认入库前核对每条数据的门店编码转换值或拒绝理由；
        预检阶段不写门店目标表，仅「确认入库」落库。
      </p>
    </header>

    <div class="precheck-controls">
      <label class="control-item">
        <span>导入批次</span>
        <select v-model.number="batchId" data-testid="precheck-batch-select">
          <option :value="0" disabled>请选择批次</option>
          <option v-for="row in batches" :key="row.id" :value="Number(row.id)">
            {{ row.id }} · {{ row['批次编号'] }}（{{ row['来源系统'] }} / {{ row['实体模型'] }}）
          </option>
        </select>
      </label>
      <label class="control-item">
        <span>分片大小</span>
        <input v-model.number="shardSize" type="number" min="1" data-testid="precheck-shard-size" />
      </label>
      <label class="control-item control-grow">
        <span>待导入数据（JSON 数组，含外部编码字段）</span>
        <textarea
          v-model="rawJson"
          rows="4"
          data-testid="precheck-raw-json"
          placeholder='[{"store_ext_code":"N-1","store_name":"一号门店"}]'
        ></textarea>
      </label>
      <div class="control-actions">
        <button class="btn primary" type="button" data-testid="precheck-split" @click="onSplit">
          切分并预检
        </button>
        <button class="btn" type="button" data-testid="precheck-refresh" @click="loadChecklist">
          刷新清单
        </button>
        <button
          class="btn"
          type="button"
          data-testid="precheck-download"
          :disabled="!batchId"
          @click="onDownload"
        >
          下载预检文件
        </button>
      </div>
    </div>

    <p v-if="message" class="precheck-message" :class="messageKind" data-testid="precheck-message">
      {{ message }}
    </p>

    <template v-if="checklist">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">预检记录</span>
          <strong class="stat-value">{{ checklist.total_rows }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">通过</span>
          <strong class="stat-value">{{ checklist.accepted }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">拒绝</span>
          <strong class="stat-value">{{ checklist.rejected }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">规则版本</span>
          <strong class="stat-value">{{ checklist.rule_versions.join(' / ') || '—' }}</strong>
        </article>
      </div>

      <div v-if="checklist.version_drift" class="drift-banner" data-testid="version-drift">
        分片命中的入库规则版本不一致（{{ checklist.rule_versions.join('、') }}），
        外部编码对应目标字段可能已变化，存在版本漂移，禁止确认入库。
      </div>

      <table class="data-table shard-table">
        <thead>
          <tr>
            <th>分片</th>
            <th>状态</th>
            <th>规则版本</th>
            <th>处理进度</th>
            <th>通过 / 拒绝</th>
            <th>尝试次数</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="shard in checklist.shards" :key="shard.shard_index">
            <td>分片{{ shard.shard_index }}</td>
            <td>{{ shard.status }}</td>
            <td>{{ shard.rule_version ?? '—' }}</td>
            <td>{{ shard.processed }} / {{ shard.shard_size }}</td>
            <td>{{ shard.accepted }} / {{ shard.rejected }}</td>
            <td>{{ shard.attempts }}</td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :data-testid="`precheck-run-${shard.shard_index}`"
                @click="onPrecheck(shard.shard_index)"
              >
                预检/补跑
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`precheck-recheck-${shard.shard_index}`"
                @click="onRecheck(shard.shard_index)"
              >
                修订后复查
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="checklist.blockers.length" class="blocker-list">
        <strong>确认入库受阻原因：</strong>
        <ul>
          <li v-for="blocker in checklist.blockers" :key="blocker">{{ blocker }}</li>
        </ul>
      </div>

      <table class="data-table checklist-table">
        <thead>
          <tr>
            <th>原数据定位</th>
            <th>分片/源行</th>
            <th>规则版本</th>
            <th>{{ storeCodeField }}</th>
            <th>结论</th>
            <th>拒绝理由</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in checklist.items"
            :key="item.row_key"
            :data-testid="`precheck-item-${item.row_key}`"
            :data-accepted="String(item.accepted)"
          >
            <td>{{ item.source_ref }}</td>
            <td>{{ item.shard_index }} / {{ item.row_no }}</td>
            <td>{{ item.rule_version }}</td>
            <td>{{ item.store_code_value || '—' }}</td>
            <td :class="item.accepted ? 'ok-text' : 'bad-text'">
              {{ item.accepted ? '通过' : '拒绝' }}
            </td>
            <td>{{ item.reject_reason || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <div class="precheck-footer">
        <button
          class="btn primary"
          type="button"
          data-testid="precheck-confirm"
          :disabled="!checklist.confirmable"
          @click="onConfirm"
        >
          确认入库
        </button>
        <span v-if="!checklist.confirmable" class="hint-text">
          需全片预检完成、无拒绝且无版本漂移后才能确认
        </span>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  confirmImport,
  downloadPrecheckReport,
  fetchPrecheckChecklist,
  precheckShard,
  recheckShard,
  splitBatch,
  type PrecheckChecklist,
} from '@/api/import-precheck'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ batches: EntryRow[] }>()

const batchId = ref(0)
const shardSize = ref(2)
const rawJson = ref('[{"store_ext_code":"N-1","store_name":"一号门店"},{"store_ext_code":"N-10","store_name":"十号门店"}]')
const checklist = ref<PrecheckChecklist | null>(null)
const message = ref('')
const messageKind = ref('ok')

const storeCodeField = computed(() => checklist.value?.items[0]?.store_code_field ?? '门店编码')

function notify(text: string, ok = true) {
  message.value = text
  messageKind.value = ok ? 'ok' : 'bad'
}

function parseRawRows(): Record<string, unknown>[] | null {
  try {
    const parsed = JSON.parse(rawJson.value) as unknown
    if (!Array.isArray(parsed)) {
      notify('待导入数据必须是 JSON 数组', false)
      return null
    }
    return parsed as Record<string, unknown>[]
  } catch {
    notify('待导入数据不是合法 JSON，请修订后重试', false)
    return null
  }
}

async function loadChecklist() {
  if (!batchId.value) return
  try {
    checklist.value = await fetchPrecheckChecklist(batchId.value)
  } catch (error) {
    notify(error instanceof Error ? error.message : '预检清单读取失败', false)
  }
}

async function onSplit() {
  if (!batchId.value) {
    notify('请先选择导入批次', false)
    return
  }
  const rawRows = parseRawRows()
  if (!rawRows) return
  try {
    const result = await splitBatch(batchId.value, shardSize.value, rawRows)
    notify(String(result.message ?? '切分完成'), Boolean(result.ok))
    await loadChecklist()
  } catch (error) {
    notify(error instanceof Error ? error.message : '切分失败', false)
  }
}

async function onPrecheck(shardIndex: number) {
  try {
    const result = await precheckShard(batchId.value, shardIndex)
    notify(String(result.message ?? '预检完成'), Boolean(result.ok))
    await loadChecklist()
  } catch (error) {
    notify(error instanceof Error ? error.message : '预检失败', false)
  }
}

async function onRecheck(shardIndex: number) {
  try {
    const result = await recheckShard(batchId.value, shardIndex)
    notify(String(result.message ?? '复查完成'), Boolean(result.ok))
    await loadChecklist()
  } catch (error) {
    notify(error instanceof Error ? error.message : '复查失败', false)
  }
}

async function onDownload() {
  try {
    await downloadPrecheckReport(batchId.value)
    notify('导入预检文件已下载')
  } catch (error) {
    notify(error instanceof Error ? error.message : '下载失败', false)
  }
}

async function onConfirm() {
  try {
    const result = await confirmImport(batchId.value)
    notify(String(result.message ?? '确认入库完成'), Boolean(result.ok))
    await loadChecklist()
  } catch (error) {
    notify(error instanceof Error ? error.message : '确认入库失败', false)
  }
}
</script>

<style scoped>
.precheck-panel {
  margin-top: 24px;
  padding: 16px;
  border: 1px solid #d7dce5;
  border-radius: 10px;
  background: #fbfcfe;
}
.precheck-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-end;
}
.control-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.control-grow {
  flex: 1 1 320px;
}
.control-item textarea,
.control-item select,
.control-item input {
  padding: 6px 8px;
  border: 1px solid #c7cedb;
  border-radius: 6px;
  font-family: inherit;
}
.control-actions {
  display: flex;
  gap: 8px;
}
.drift-banner {
  margin: 12px 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #fff4e5;
  border: 1px solid #f0b35e;
  color: #9a5b12;
}
.blocker-list {
  margin: 12px 0;
  color: #b03a3a;
}
.blocker-list ul {
  margin: 6px 0 0 18px;
}
.precheck-message {
  margin: 12px 0 0;
}
.precheck-message.ok {
  color: #237a4b;
}
.precheck-message.bad {
  color: #b03a3a;
}
.ok-text {
  color: #237a4b;
}
.bad-text {
  color: #b03a3a;
}
.hint-text {
  margin-left: 10px;
  color: #7a8190;
  font-size: 13px;
}
.precheck-footer {
  margin-top: 14px;
  display: flex;
  align-items: center;
}
</style>
