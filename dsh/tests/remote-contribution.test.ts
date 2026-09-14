/**
 * Unit tests for the browser Remote contribution (`src/client/remote.ts`).
 *
 * Verifies the `atomMemory` descriptors mount correctly for the Gateway client:
 * every method is `direct`, carries only strict JSON codecs (the client mount
 * rejects plain `src-json` codecs), and exposes the exact method set the Host
 * `AtomMemoryController` marks with `@Remote`.
 */
import { describe, expect, it } from 'vitest'
import {
  ATOM_MEMORY_REMOTE,
  REMOTE_NAMESPACE,
} from '../src/client/remote.ts'

describe('ATOM_MEMORY_REMOTE', () => {
  it('targets the atomMemory namespace with all Host-exposed methods', () => {
    expect(REMOTE_NAMESPACE).toBe('atomMemory')
    expect(ATOM_MEMORY_REMOTE.package).toBe('dsh-atom-memory')
    const methods = ATOM_MEMORY_REMOTE.descriptors.map(d => d.method)
    expect(methods).toEqual([
      'listFacts',
      'editFact',
      'deleteFact',
      'memoryMd',
      'listProfile',
      'upsertProfile',
      'deleteProfile',
      'backup',
      'restore',
      'getRuntime',
    ])
    for (const descriptor of ATOM_MEMORY_REMOTE.descriptors) {
      expect(descriptor.namespace).toBe('atomMemory')
    }
  })

  it('uses only strict JSON codecs so the Gateway client accepts the mount', () => {
    for (const descriptor of ATOM_MEMORY_REMOTE.descriptors) {
      expect(descriptor.invocation).toEqual({ kind: 'direct' })
      expect(descriptor.result.mode).toBe('strict')
      for (const parameter of descriptor.parameters) {
        const codec = parameter.codec
        expect(codec.mode).toBe('strict')
        expect(parameter.source).toBe('json')
        if (codec.mode === 'strict') {
          // Strict-plus-JSON: parse is a pass-through, never a narrowing loss.
          expect(typeof codec.schema.parse).toBe('function')
        }
      }
    }
  })

  it('gives every arg-carrying method a single named wire field for `args`', () => {
    for (const descriptor of ATOM_MEMORY_REMOTE.descriptors) {
      if (descriptor.method === 'getRuntime') {
        expect(descriptor.parameters).toHaveLength(0)
        continue
      }
      expect(descriptor.parameters).toHaveLength(1)
      expect(descriptor.parameters[0]?.name).toBe('args')
      expect(descriptor.parameters[0]?.wire).toBe('args')
    }
  })

  it('matches the Host @Remote marker set exactly', async () => {
    // Independent source-of-truth: parse the Host controller source for @Remote.
    const source = await import('node:fs/promises').then(m =>
      m.readFile(new URL('../src/controller.ts', import.meta.url), 'utf8'))
    const marked: string[] = []
    for (const match of source.matchAll(/@Remote[\s\S]*?\n\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(/g)) {
      marked.push(match[1])
    }
    expect([...ATOM_MEMORY_REMOTE.descriptors].map(d => d.method).sort())
      .toEqual([...marked].sort())
  })
})
