<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const boundaries = ref([])
const watermarks = ref([])
const watermarkOk = ref(true)
const err = ref('')

function watermarkOf(b) {
  return watermarks.value.find((w) => w.edge_id === b.id) || null
}

async function load() {
  err.value = ''
  try {
    rows.value = await api.proofs()
    boundaries.value = await api.gapBoundaries()
    const cert = await api.gapWatermarkCertificates()
    watermarks.value = cert.rows
    watermarkOk.value = cert.ok
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
      <h1>切分边界核对</h1>
      <p class="muted">
        每次超时切开的相邻会话对落一条边界：上段末击点、下段首击点、间隙秒与当时超时快照，三数可对；
        同一事务内已抬该访客间隙水位，下列「间隙水位核对」逐边互证。
      </p>
      <p v-if="err" class="err">{{ err }}</p>
      <p v-if="!boundaries.length && !err" class="muted">尚无切分边界：跑一批超间隙收流后在此核对。</p>
      <table v-if="boundaries.length" class="data">
        <thead>
          <tr>
            <th>边界</th><th>访客</th><th>上段 → 下段</th>
            <th>上段末击点</th><th>下段首击点</th>
            <th>间隙秒（重算）</th><th>当时超时</th><th>间隙水位</th><th>核验</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in boundaries" :key="b.id">
            <td class="mono">#{{ b.id }}</td>
            <td>{{ b.visitor_id }}</td>
            <td class="mono">{{ b.prev_session_id }} → {{ b.next_session_id }}</td>
            <td class="mono">{{ b.prev_last_hit_id }} @ {{ b.prev_last_ts }}</td>
            <td class="mono">{{ b.next_first_hit_id }} @ {{ b.next_first_ts }}</td>
            <td class="mono">{{ b.gap_sec }}s（{{ b.check.gap_sec_recomputed }}s）</td>
            <td class="mono">{{ b.timeout_sec_snapshot }}s</td>
            <td class="mono">
              <template v-if="watermarkOf(b)">
                {{ watermarkOf(b).watermark_sec }}s
                <span :class="watermarkOf(b).ok ? 'ok' : 'err'">
                  {{ watermarkOf(b).ok ? '✓ 水位=边 gap' : '✗ 分叉' }}
                </span>
              </template>
              <span v-else class="err">✗ 未抬水位</span>
            </td>
            <td>
              <span v-if="b.check.ok" class="ok">✓ 三数一致</span>
              <span v-else class="err">✗ 矛盾</span>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h1>间隙水位核对</h1>
      <p class="muted">
        每条切分边界在落边的同一事务内抬一行间隙水位（水位值 = 边 gap_sec，记边键与下段首击触发键）。
        此处为边界页摘要：
        <span v-if="watermarkOk" class="ok">全部水位与切边互证通过。</span>
        <span v-else class="err">存在水位与切边未接或分叉的边。</span>
        <router-link to="/watermark" class="linkish">前往水位核证页逐条核对 →</router-link>
      </p>
    </section>

    <section class="panel">
      <h1>会话核对</h1>
      <p class="muted">会话击点集合与起止须与织机汇总可核。</p>
      <table class="data">
        <thead>
          <tr><th>会话</th><th>访客</th><th>点数</th><th>起止</th><th>击点集</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td class="mono">{{ r.session_id }}</td>
            <td>{{ r.visitor_id }}</td>
            <td>{{ r.hit_count }}</td>
            <td class="mono">{{ r.started_at }} → {{ r.ended_at }}</td>
            <td class="mono">{{ r.hit_ids_json }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
div > .panel + .panel {
  margin-top: 1rem;
}
tr.rowerr td {
  background: #fbeeee;
}
.linkish {
  color: var(--copper);
  margin-left: 0.4rem;
  white-space: nowrap;
}
</style>
