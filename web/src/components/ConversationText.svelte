<script lang="ts">
  import { findParts, type FindMatch } from '../lib/conversation-find'
  let { text, matches, selectedIndex }: { text: string; matches: FindMatch[]; selectedIndex: number } = $props()
  const parts = $derived(findParts(text, matches, selectedIndex))
</script>

{#each parts as part}{#if part.match === null}{part.text}{:else}<mark class:find-current={part.selected} data-conversation-match={part.match} data-selected={part.selected ? 'true' : undefined} aria-current={part.selected ? 'true' : undefined}>{part.text}</mark>{/if}{/each}
