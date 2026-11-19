<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const boundaries = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await api.proofs()
    boundaries.value = await api.gapBoundaries()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <!-- plant-ui -->

  <div>
    <section class="panel">
      <h1>切分边界核对</h1>
      <p class="muted">
        每次超时切开的相邻会话对落一条边界：上段末击点、下段首击点、间隙秒与当时超时快照，三数可对。
      </p>
      <p v-if="err" class="err">{{ err }}</p>
      <p v-if="!boundaries.length && !err" class="muted">尚无切分边界：跑一批超间隙收流后在此核对。</p>
      <table v-if="boundaries.length" class="data">
        <thead>
          <tr>
            <th>边界</th><th>访客</th><th>上段 → 下段</th>
            <th>上段末击点</th><th>下段首击点</th>
            <th>间隙秒（重算）</th><th>当时超时</th><th>核验</th>
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
            <td>
              <span v-if="b.check.ok" class="ok">✓ 三数一致</span>
              <span v-else class="err">✗ 矛盾</span>
            </td>
          </tr>
        </tbody>
      </table>
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
</style>
