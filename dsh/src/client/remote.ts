/**
 * Browser-half Remote contribution for the `atomMemory` namespace.
 *
 * The Host side (`../controller.ts`) exposes the memory panel's data operations
 * as `@Remote` methods under the `atomMemory` wire namespace. The browser has no
 * auto-generated `ctx.remote.atomMemory` — the harness only mounts the
 * contributions it explicitly selects. This module hands that same namespace's
 * descriptors to `ctx.remote.$mount(...)`, so the browser-side
 * `ctx.remote.atomMemory.<method>` stubs exist and call through the Gateway to
 * the Host `AtomMemoryController`.
 *
 * Descriptors here are mirror-images of the Host SRC claims: every method takes
 * a single JSON `args` payload and returns a JSON business value, so a shared
 * pass-through strict codec suffices (the wire fully round-trips plain JSON).
 *
 * @module dsh-atom-memory/client/remote
 */
import type {
  InvocationDescriptor,
  TypertCodec,
  TypertRemoteContribution,
  TypertSchema,
} from '@deepseek-ai/dsh-typert-protocol'

/** The Remote wire namespace this browser half mounts (matches the Host binding). */
export const REMOTE_NAMESPACE = 'atomMemory'

/**
 * A pass-through strict boundary codec. The Gateway client requires
 * `mode: 'strict'` with a `schema.parse` (see `requireStrictCodec` /
 * `parseInput` in `@deepseek-ai/dsh-api-remotes/client`); the Host SRC claim
 * for the same endpoint uses `{ mode: 'src-json' }`, so both ends agree the
 * value is plain JSON that needs no structural narrowing here.
 */
const JSON_SCHEMA: TypertSchema<unknown> = { parse: (value: unknown) => value }

const JSON_CODEC: TypertCodec = {
  mode: 'strict',
  typeSymbol: 'dsh-atom-memory#JsonValue',
  schema: JSON_SCHEMA,
}

/** One descriptor for a Host method whose single argument is a JSON `args` object. */
function jsonArgsMethod(method: string, hasArgs: boolean): InvocationDescriptor {
  return {
    id: `${REMOTE_NAMESPACE}/${method}`,
    service: 'atomMemoryController',
    namespace: REMOTE_NAMESPACE,
    method,
    invocation: { kind: 'direct' },
    parameters: hasArgs
      ? [{ name: 'args', wire: 'args', source: 'json', codec: JSON_CODEC }]
      : [],
    result: JSON_CODEC,
  }
}

/** The `atomMemory` contribution mounted by this browser half. */
export const ATOM_MEMORY_REMOTE: TypertRemoteContribution = {
  package: 'dsh-atom-memory',
  descriptors: [
    jsonArgsMethod('listFacts', true),
    jsonArgsMethod('editFact', true),
    jsonArgsMethod('deleteFact', true),
    jsonArgsMethod('memoryMd', true),
    jsonArgsMethod('summary', true),
    jsonArgsMethod('listProfile', true),
    jsonArgsMethod('upsertProfile', true),
    jsonArgsMethod('deleteProfile', true),
    jsonArgsMethod('backup', true),
    jsonArgsMethod('restore', true),
    jsonArgsMethod('getRuntime', false),
  ],
}
