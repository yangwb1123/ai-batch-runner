import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'
import { fileURLToPath } from 'node:url'

const source = (name: string, entry = 'index.ts') =>
  fileURLToPath(new URL(`./.iris/packages/${name}/src/${entry}`, import.meta.url))

export default defineConfig({
  plugins: [svelte()],
  // Escape literal whitespace without changing string or tagged-template values.
  esbuild: { supported: { 'template-literal': false } },
  base: '/web/assets/',
  resolve: {
    conditions: ['browser'],
    dedupe: ['svelte'],
    alias: [
      { find: /^@iris-ui-kit\/svelte\/(.+)$/, replacement: source('svelte', '$1') },
      { find: /^@iris-ui-kit\/core\/(.+)$/, replacement: source('core', '$1.ts') },
      ...['svelte', 'core', 'theme', 'tokens', 'icons', 'skins'].map((name) => ({
        find: `@iris-ui-kit/${name}`, replacement: source(name),
      })),
    ],
  },
  server: { host: '127.0.0.1', strictPort: true },
  build: {
    sourcemap: false,
    target: 'es2022',
    assetsInlineLimit: 0,
    rollupOptions: {
      output: { inlineDynamicImports: true, entryFileNames: 'app.js', assetFileNames: 'app[extname]' },
    },
  },
})
