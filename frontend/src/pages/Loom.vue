<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const overview = ref(null)
const sessions = ref([])
const details = ref({})
const boundaries = ref([])
const err = ref('')

function gapOf(sessionId) {
  return boundaries.value.find((b) => b.next_session_id === sessionId)
}

async function load() {
  err.value = ''
  try {
    overview.value = await api.overview()
    sessions.value = await api.sessions()
    boundaries.value = await api.gapBoundaries()
    const map = {}
    for (const s of sessions.value) {
      map[s.id] = await api.session(s.id)
    }
    details.value = map
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(load)
</script>

<template>
  <!-- plant-ui -->

  <div>
    <div class="stats" v-if="overview">
      <div class="stat"><b>{{ overview.visitors }}</b><span>访客</span></div>
      <div class="stat"><b>{{ overview.sessions }}</b><span>会话</span></div>
      <div class="stat"><b>{{ overview.hits_primary }}</b><span>正式击点</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
      <div class="stat"><b>{{ overview.gap_boundaries }}</b><span>切分边界</span></div>
      <div class="stat"><b>{{ overview.idle_timeout_sec }}s</b><span>空闲超时</span></div>
    </div>
    <p v-if="err" class="err">{{ err }}</p>

    <section class="panel">
      <h1>会话织机</h1>
      <p class="muted">按空闲间隙织成会话带；超隙切开处标注边界，核对页可对三数。</p>
      <div class="lane-list">
        <article v-for="s in sessions" :key="s.id" class="lane">
          <div class="lane-head">
            <div>
              <strong>{{ s.id }}</strong>
              <span class="muted"> · {{ s.visitor_id }} · {{ s.status }}</span>
            </div>
            <span class="mono">{{ s.hit_count }} 点 · {{ s.started_at }} → {{ s.ended_at || '…' }}</span>
          </div>
          <div v-if="gapOf(s.id)" class="gap-note">
            ⚡ 超隙切开：{{ gapOf(s.id).prev_session_id }} 末击
            <span class="mono">{{ gapOf(s.id).prev_last_hit_id }} @ {{ gapOf(s.id).prev_last_ts }}</span>
            → 本段首击
            <span class="mono">{{ gapOf(s.id).next_first_hit_id }} @ {{ gapOf(s.id).next_first_ts }}</span>
            · 间隙 <b class="mono">{{ gapOf(s.id).gap_sec }}s</b> ＞ 当时超时
            <b class="mono">{{ gapOf(s.id).timeout_sec_snapshot }}s</b>
            · 边界 <span class="mono">#{{ gapOf(s.id).id }}</span>
          </div>
          <div class="beads" v-if="details[s.id]">
            <template v-for="(h, i) in details[s.id].hits" :key="h.id">
              <span v-if="i" class="connector" aria-hidden="true"></span>
              <span class="hit-bead">
                <strong>{{ h.event_name }}</strong>
                <span>{{ h.page_path }}</span>
              </span>
            </template>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
