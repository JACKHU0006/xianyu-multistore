<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '—' },
  detail: { type: String, default: '' },
  tone: { type: String, default: '' }, // ok | warn | danger
})
</script>

<template>
  <div class="stat">
    <div class="k">{{ label }}</div>
    <div class="v" :class="tone">{{ value }}</div>
    <div v-if="detail" class="d">{{ detail }}</div>
  </div>
</template>
