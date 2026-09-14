/**
 * Downlevel legacy/stage-3 method decorators in the emitted node bundle.
 *
 * The dsh host runs plain Node ESM, which does NOT support the decorator syntax
 * that rolldown/tsdown emits verbatim for `@Remote`-style decorated controllers
 * (Node < 27 rejects the file with "Invalid or unexpected token"). The harness
 * solves this by pre-transpiling TS with tsc (its `lib/types` is already
 * decorator-free); this plugin is the standalone-repo equivalent: it runs the
 * emitted lib/index.mjs through Babel's 2023-11 decorator transform (the same
 * `_applyDecs`/`__esDecorate` helpers) so the bundle stays plain, loadable JS.
 *
 * No-op when the bundle already contains no decorator syntax.
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, dirname } from 'node:path'
import { transformSync } from '@babel/core'
import decorators from '@babel/plugin-proposal-decorators'

const here = dirname(fileURLToPath(import.meta.url))
const target = join(here, '..', 'lib', 'index.mjs')

const source = readFileSync(target, 'utf8')

// A leading-@-at-statement decorator (opts out of the more common usages of @
// inside strings/comments in a bundled single-line-first artifact).
if (!/\n\s*@[A-Za-z_$]/.test(source)) {
  process.stdout.write('transpile-decorators: no decorator syntax, skipping\n')
  process.exit(0)
}

const result = transformSync(source, {
  filename: target,
  configFile: false,
  babelrc: false,
  sourceType: 'module',
  compact: true,
  // Only the decorator transform — leave everything else untouched.
  plugins: [[decorators, { version: '2023-11' }]],
})

if (!result || typeof result.code !== 'string') {
  process.stderr.write('transpile-decorators: babel failed (no output)\n')
  process.exit(1)
}

writeFileSync(target, result.code)
process.stdout.write(`transpile-decorators: downleveled decorators -> ${target}\n`)
