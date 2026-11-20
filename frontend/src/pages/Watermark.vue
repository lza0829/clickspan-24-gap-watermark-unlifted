<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const allOk = ref(false)
const visitors = ref([])
const picked = ref('')
const err = ref('')

const visitorIds = computed(() => {
  const ids = new Set(rows.value.map((r) => r.visitor_id))
  for (const v of visitors.value) ids.add(v.id)
  return [...ids].sort()
})

async function load() {
  err.value = ''
  try {
    const [cert, vs] = await Promise.all([
      api.gapWatermarkCertificates(picked.value || undefined),
      api.visitors(),
    ])
    rows.value = cert.rows
    allOk.value = cert.ok
    visitors.value = vs
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(load)
</script>

<template>
  <!-- plant-ui -->

  <div>
    <section class="panel">
      <h1>间隙水位核证</h1>
      <p class="muted">
        每条超隙切边在落边的同一事务内抬一行间隙水位：水位值取当次切边的 gap_sec，
        并记边键（访客+上段→下段）与触发键（下段首击）。此处只读回读切边互证，
        缺水位即「切边已落、水位未抬」，水位与边分叉或挂错访客一律判失败。
      </p>
      <div class="filters">
        <label>
          访客
          <select v-model="picked" class="mono" @change="load">
            <option value="">全部</option>
            <option v-for="v in visitorIds" :key="v" :value="v">{{ v }}</option>
          </select>
        </label>
      </div>
      <p v-if="err" class="err">{{ err }}</p>
      <p v-else-if="!rows.length" class="muted">尚无间隙水位：跑一批超间隙收流后在此核对。</p>
      <p v-else-if="allOk" class="ok">全部水位与切边互证通过（水位 = 边 gap）。</p>
      <p v-else class="err">存在水位与切边未接或分叉的边，见下表标红行。</p>

      <table v-if="rows.length" class="data">
        <thead>
          <tr>
            <th>访客</th><th>边键</th><th>边</th><th>gap_sec</th>
            <th>水位</th><th>触发击点</th><th>结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="w in rows" :key="(w.edge_id || 'none') + ':' + w.visitor_id" :class="{ rowerr: !w.ok }">
            <td>{{ w.visitor_id }}</td>
            <td class="mono">{{ w.edge_key || '—' }}</td>
            <td class="mono">
              <template v-if="w.edge_id">#{{ w.edge_id }}</template>
              <span v-else class="err">缺边</span>
            </td>
            <td class="mono">{{ w.gap_sec === null ? '—' : w.gap_sec + 's' }}</td>
            <td class="mono">{{ w.watermark_sec === null ? '未抬' : w.watermark_sec + 's' }}</td>
            <td class="mono">{{ w.trigger_hit_id || '—' }}</td>
            <td>
              <span v-if="w.ok" class="ok">✓ {{ w.conclusion }}</span>
              <span v-else class="err">✗ {{ w.conclusion }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  gap: 1rem;
  margin-bottom: 0.6rem;
  align-items: center;
}
.filters select {
  margin-left: 0.35rem;
  padding: 0.25rem 0.4rem;
}
tr.rowerr td {
  background: #fbeeee;
}
</style>
