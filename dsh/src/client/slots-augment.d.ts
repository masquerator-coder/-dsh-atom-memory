/**
 * Local `ctx.slots` augmentation.
 *
 * The `slots` service runtime is provided by the dsh web composition; its
 * `Context` augmentation is not visible to a standalone plugin typecheck (it
 * lives in the harness assembly). We redeclare the seam here with the same
 * `SlotCore` type so client TSX typechecks outside the monorepo.
 */
import type { SlotCore } from '@deepseek-ai/dsh-client-ui-slots'

declare module '@deepseek-ai/cordis' {
  interface Context {
    /**
     * Slot registry service. `register` comes from `SlotCore`; `inject` is the
     * cordis provider seam the composition wraps around it to group a plugin's
     * contributions declaratively (the callback runs one or more `register`s).
     */
    slots: SlotCore & {
      inject(name: string, fn: (() => unknown) | (() => Generator<unknown, void, unknown>)): void
    }
  }
}

export {}
