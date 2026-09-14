/**
 * Host Remote controller exposing the dsh-atom-memory operations to the
 * browser settings panel.
 *
 * The panel edits memory / user profile and drives backup & restore in the
 * Python store; those are data operations, not scalar namespaces, so they are
 * carried over the dsh Remote gateway rather than the settings document. This
 * controller is a thin, structured bridge to the Python child process — it
 * never reads or synthesises model-visible content itself, satisfying the
 * "model-visible <=> logged" invariant by leaving all memory logic in Python.
 *
 * @module dsh-atom-memory/controller
 */
import { Context } from '@deepseek-ai/cordis'
import { Remote, TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
import type { PythonBridge } from './bridge.ts'
import type { LiveRuntime, Runtime } from './runtime.ts'

/** One active fact as the panel edits it. */
export interface FactEditInput {
  fact_id: string
  subject?: string
  predicate?: string
  object?: string
  content?: string
  type?: string
}

/** One profile row as the panel edits it. */
export interface ProfileEditInput {
  section: string
  key?: string
  value: string
}

declare module '@deepseek-ai/cordis' {
  interface Context {
    /** Host owner of the `atom-memory` Remote namespace for the memory panel. */
    atomMemoryController: AtomMemoryController
  }
}

/**
 * Host service backing `ctx.remote.atomMemory`. Every method delegates to the
 * Python bridge and returns a JSON-serializable business value (backup payloads
 * are plain JSON). Arguments are validated minimally here and fully by the
 * Python side.
 */
export class AtomMemoryController extends TypertRemoteService {
  constructor(ctx: Context, private readonly bridge: PythonBridge, private readonly runtime: Runtime) {
    super(ctx, 'atomMemoryController', { namespace: 'atom-memory' })
  }

  /** Whether the bridge is alive and the plugin master switch is on. */
  private assertReady(): void {
    if (!this.runtime.isEnabled()) throw new Error('memory is disabled')
    if (!this.bridge.alive) throw new Error('memory bridge is not running')
  }

  /** Paginate the user's active facts. */
  @Remote
  async listFacts(args: { user: string; offset?: number; limit?: number; includeRetracted?: boolean }):
  Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('list_facts', {
      user_id: args.user,
      offset: args.offset ?? 0,
      limit: args.limit ?? 50,
      include_retracted: args.includeRetracted ?? false,
    }) as Promise<Record<string, unknown>>
  }

  /** Directly edit one active fact's SPO / type / content. */
  @Remote
  async editFact(args: { user: string } & FactEditInput): Promise<Record<string, unknown>> {
    this.assertReady()
    if (!args.fact_id) throw new Error('editFact requires fact_id')
    return this.bridge.call('edit_fact', {
      user_id: args.user,
      fact_id: args.fact_id,
      subject: args.subject,
      predicate: args.predicate,
      object: args.object,
      content: args.content,
      type: args.type,
    }) as Promise<Record<string, unknown>>
  }

  /** List the user's profile rows. */
  @Remote
  async listProfile(args: { user: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('list_profile', { user_id: args.user }) as Promise<Record<string, unknown>>
  }

  /** Add or update one profile row. */
  @Remote
  async upsertProfile(args: { user: string } & ProfileEditInput): Promise<Record<string, unknown>> {
    this.assertReady()
    if (!args.section || !args.key) throw new Error('upsertProfile requires section and key')
    return this.bridge.call('upsert_profile', {
      user_id: args.user,
      section: args.section,
      key: args.key,
      value: args.value,
    }) as Promise<Record<string, unknown>>
  }

  /** Delete one profile row. */
  @Remote
  async deleteProfile(args: { user: string; section: string; key: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('delete_profile', {
      user_id: args.user,
      section: args.section,
      key: args.key,
    }) as Promise<Record<string, unknown>>
  }

  /** Export the user's memory as a JSON snapshot (for download). */
  @Remote
  async backup(args: { user: string }): Promise<Record<string, unknown>> {
    this.assertReady()
    return this.bridge.call('backup', { user_id: args.user }) as Promise<Record<string, unknown>>
  }

  /** Import a JSON snapshot, replacing the user's memory. */
  @Remote
  async restore(args: { user: string; payload: Record<string, unknown> }): Promise<Record<string, unknown>> {
    this.assertReady()
    if (!args.payload || typeof args.payload !== 'object') throw new Error('restore requires a backup payload')
    return this.bridge.call('restore', {
      user_id: args.user,
      payload: args.payload,
    }) as Promise<Record<string, unknown>>
  }

  /** Read the current live runtime (enabled / capture / model override). */
  @Remote
  async getRuntime(): Promise<LiveRuntime> {
    return this.runtime.get()
  }
}
