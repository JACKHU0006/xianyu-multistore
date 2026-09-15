<script setup>
defineProps({
  tone: { type: String, default: 'idle' }, // ok | warn | danger | accent | idle
})
</script>

<template>
  <span class="pill" :class="tone">
    <slot />
  </span>
</template>
