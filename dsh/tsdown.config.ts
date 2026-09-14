import { defineConfig, type UserConfig } from 'tsdown'

// Two faces:
//   1. lib/index.mjs — the node host half loaded by the dsh Loader (plain ESM).
//   2. lib/client.js  — the browser client bundle, emitted in the harness's
//      module-table format (`window.__ModuleLoader__.load({id, factory(require)})`)
//      that the web client-modules service serves directly at exports["./client"].
//
// The browser bundle must leave every module-table row (react, cordis,
// dsh-client-*) as a `require()` resolved through the injected table require;
// everything else (this plugin's own code, inline-safe layers) is inlined.

const PLATFORM_MODULES = [
  'react', 'react/jsx-runtime', 'react-dom', 'react-dom/client', '@deepseek-ai/cordis',
  '@deepseek-ai/dsh-client-store',
  '@deepseek-ai/dsh-client-ui-slots',
  '@deepseek-ai/dsh-client-ui-primitives',
  '@deepseek-ai/dsh-client-ui-dockkit',
]

// dsh.client.external — the same rows plus the collaboration packages this
// client injects (mirrors package.json).
const CLIENT_EXTERNALS = new Set([
  ...PLATFORM_MODULES,
  '@deepseek-ai/dsh-client-locale',
  '@deepseek-ai/dsh-client-ui-settings',
  '@deepseek-ai/dsh-client-ui-renderer',
  '@deepseek-ai/dsh-api-remotes',
])

const isRequested = (specifier: string): boolean => CLIENT_EXTERNALS.has(specifier)

// Keep rollup/tsdown happy that `UserConfig` is a used import for typing.
type _T = UserConfig

export default defineConfig([
  {
    // Node host half: bundles src/index.ts into lib/index.mjs.
    entry: { index: 'src/index.ts' },
    format: ['esm'],
    platform: 'node',
    target: 'node22',
    dts: true,
    clean: true,
    sourcemap: false,
    outDir: 'lib',
  },
  {
    // Browser client half. `clean` stays off so it doesn't wipe the node
    // output emitted above (mirrors the harness).
    name: 'dsh-atom-memory/client',
    entry: { client: 'src/client/index.ts' },
    outDir: 'lib',
    format: 'cjs',
    platform: 'browser',
    dts: false,
    sourcemap: true,
    clean: false,
    tsconfig: 'tsconfig.client.json',
    deps: {
      // Module-table rows stay as require() calls the injected table answers.
      neverBundle: isRequested,
      // Anything else (this plugin's code, inline-safe layers) is bundled.
      alwaysBundle: (specifier: string) => !isRequested(specifier),
    },
    define: {
      'process.env.NODE_ENV': JSON.stringify(process.env.NODE_ENV ?? 'production'),
      'import.meta.env.MODE': JSON.stringify(process.env.NODE_ENV ?? 'production'),
      'import.meta.env': JSON.stringify({ MODE: process.env.NODE_ENV ?? 'production' }),
    },
    outputOptions: {
      entryFileNames: 'client.js',
      banner: 'window.__ModuleLoader__.load({ id: "dsh-atom-memory", factory: (require) => {',
      footer: 'return module.exports; } });',
      intro: 'var module = { exports: {} }; var exports = module.exports;',
    },
  },
])
