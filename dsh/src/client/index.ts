/**
 * dsh-atom-memory — browser half. Contributes a single **记忆** (Memory)
 * settings section to the dsh settings panel.
 *
 * Node half (`../index.ts`) registers the `atom-memory` settings namespace and
 * the `atom-memory` Remote operations; this browser half binds the namespace
 * (features 1 & 2) and calls the Remote operations (features 3-5). It never
 * reads or fabricates model-visible memory content itself.
 *
 * @module dsh-atom-memory/client
 */
import type { Context } from '@deepseek-ai/cordis'
// Type-only: pulls the client Context merges (ctx.locale, ctx.settingsScope,
// ctx.slots, ctx.remote) from the composed packages.
import type {} from '@deepseek-ai/dsh-client-locale/client'
import type {} from '@deepseek-ai/dsh-client-ui-settings/client'
import type {} from '@deepseek-ai/dsh-client-ui-slots'
import type {} from '@deepseek-ai/dsh-api-remotes/client'
import { MemorySettingsController } from './memory-settings-controller.ts'
import { MemorySettingsSection } from './MemorySettingsSection.tsx'
import { dicts, LOCALE_NS } from './locales.ts'
import { ATOM_MEMORY_REMOTE } from './remote.ts'

/** The settings namespace registered by the Host plugin. */
const SETTINGS_NAMESPACE = 'atom-memory'

/** Required services (cordis fiber inject). */
export const inject = ['slots', 'locale', 'settingsScope', 'remote'] as const

/**
 * Mount the memory settings section.
 * @param ctx - the browser plugin context.
 */
export async function apply(ctx: Context): Promise<() => void> {
  const t = ctx.locale.bind(LOCALE_NS)
  ctx.effect(() => ctx.locale.register(LOCALE_NS, dicts), 'atom-memory: section dictionaries')

  // The Host `AtomMemoryController` is not one of the `@deepseek-ai/dsh-api-remotes`
  // assembly namespaces, so the browser has no auto-generated descriptors for it.
  // Mount them here, then hand the namespace service to the controller.
  const disposeRemote = await ctx.remote.$mount(ATOM_MEMORY_REMOTE)

  const controller = new MemorySettingsController(
    ctx.settingsScope.bind({ namespace: SETTINGS_NAMESPACE }),
    ctx.remote.atomMemory as unknown,
  )
  ctx.effect(() => () => { controller.dispose() }, 'atom-memory: controller')

  // A single 记忆 row in the Settings sidebar.
  ctx.slots.inject('settings.section', () => ctx.slots.register({
    name: 'settings.section',
    id: 'memory',
    order: 60,
    label: () => t('title'),
    locale: LOCALE_NS,
    inject: () => controller.inject(),
  }, MemorySettingsSection))

  return async () => {
    controller.dispose()
    await disposeRemote()
  }
}
