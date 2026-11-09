import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Run } from '../types'
import {
  absolute,
  apply,
  ctlArgv,
  fresh,
  isFinal,
  lines,
  merged,
  monitorArgv,
  outOfCommand,
  parseItem,
  startReply,
  statusText,
  summary,
} from './runs'

const PANE = 'vivarium-runs'
const TITLE = 'vivarium'
// Not `vivarium`: that is the vivarium skill's command.
const COMMAND = 'vivarium-runs'
// Change the tag when a Run's fields change: it declines what an earlier
// version stored, which outlives a reload.
const runs = atom({ plugin: 'vivarium-runs', key: 'runs' } as const, [], { shape: 'runs-1' })

// Each monitor this copy of the module started, by `out`: ending one ends
// its child. A reload starts over, and `session.start` arms them again.
const watching = new Map<string, AsyncGenerator<unknown, unknown>>()

type Run$ = EngineInterface

const change = (run$: Run$, out: string, fn: (run: Run) => Run) =>
  update(run$, runs, list => list.map(run => (run.out === out ? fn(run) : run)))

let written: string | undefined

/** The status line holds the newest live run, coarse, and is written only
 * when that text changes; with no live run it is cleared. */
const showStatus = async ($: Run$) => {
  const text = statusText(await read($, runs))
  if (text === written) return
  written = text
  $.ui.status(text)
}

/** What `run.json` says, once the run has written it: its CLI and phases. */
const runFile = async ($: Run$, out: string): Promise<{ cli?: string; phases: string[] }> => {
  try {
    const raw: unknown = JSON.parse(await $.fs.read(`${out}/run.json`))
    if (typeof raw !== 'object' || raw === null) return { phases: [] }
    const doc = raw as { vivarium?: unknown; test?: { phases?: unknown } | null }
    const phases = Array.isArray(doc.test?.phases) ? doc.test.phases.filter(p => typeof p === 'string') : []
    return { cli: typeof doc.vivarium === 'string' ? doc.vivarium : undefined, phases }
  } catch {
    // No run.json yet: the run is still evaluating, or never started.
    return { phases: [] }
  }
}

const watch = ($: Run$, run: Run) => {
  if (watching.has(run.out)) return
  const child = $.process.spawn({ argv: monitorArgv(run) })
  watching.set(run.out, child)
  void (async () => {
    let buffer = ''
    let named = false
    try {
      for await (const piece of child) {
        if (piece.stream !== 'stdout') continue
        const cut = lines(buffer, piece.text)
        buffer = cut.rest
        for (const line of cut.lines) {
          const item = parseItem(line)
          if (!item) continue
          // The first event from the run itself: `run.json` names its phases
          // by then. The monitor prints no `live` marker to wait for.
          if (!named && item.event !== 'building' && item.event !== 'builds') {
            const known = await runFile($, run.out)
            if (known.phases.length > 0) {
              named = true
              await change($, run.out, r => ({ ...r, phases: merged(known.phases, r.phases) }))
            }
          }
          await change($, run.out, r => apply(r, item))
          if (item.event === 'paused') $.ui.toast(`${run.id}: paused ${item.reason ?? ''}`.trim())
          if (item.event === 'finished') $.ui.toast(`${run.id}: ${item.text ?? 'finished'}`)
          await showStatus($)
        }
      }
    } catch (error) {
      await change($, run.out, r => ({ ...r, status: isFinal(r) ? r.status : 'gone', error: String(error) }))
    } finally {
      watching.delete(run.out)
      await change($, run.out, r => (isFinal(r) ? r : { ...r, status: 'gone' }))
      await showStatus($)
    }
  })()
}

const add = async ($: Run$, out: string, cli?: string[]) => {
  const known = await runFile($, out)
  const program = cli ?? [known.cli ?? 'vivarium']
  const run = fresh(out, program, known.phases)
  // A new run in the same `out` replaces the old one.
  watching.get(out)?.return(undefined)
  await update($, runs, list => [...list.filter(r => r.out !== out), run].slice(-8))
  void $.ui.open({ id: PANE, title: TITLE })
  watch($, run)
  await showStatus($)
}

const send = async ($: Run$, run: Run, op: string) => {
  const done = await $.process.run(ctlArgv(run, op), { timeoutMs: 60_000 })
  const said = (done.exitCode === 0 ? done.stdout : done.stderr || done.stdout).trim()
  $.ui.toast(`${run.id} ${op}: ${said.split('\n')[0] || (done.exitCode === 0 ? 'ok' : 'failed')}`)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({
      name: COMMAND,
      description: 'Follow a vivarium run in a pane: /vivarium-runs <out>; with no argument, open the pane',
    })
    for (const run of await read($, runs)) if (!isFinal(run)) watch($, run)
    await showStatus($)
    return started
  })

  on('command.run', { command: COMMAND }, async ($, e) => {
    const target = e.args.trim()
    if (target === '') {
      await $.ui.open({ id: PANE, title: TITLE })
      return { text: 'vivarium pane opened.' }
    }
    const out = absolute(target, await $.session.cwd())
    await add($, out)
    return { text: `Following the vivarium run in ${out}.` }
  })

  on('tool.call', async ($, e, next) => {
    if (e.tool === 'Bash') {
      const found = outOfCommand(e.command)
      if (found !== undefined) await add($, absolute(found, await $.session.cwd()))
      return next(e)
    }
    const ran = await next(e)
    if (e.tool === 'mcp__vivarium__start' && !ran.deny && ran.text) {
      const started = startReply(ran.text)
      if (started) await add($, started.out, started.cli)
    }
    return ran
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const list = [...(await read($, runs))].reverse()
    if (list.length === 0) {
      return <Text dimColor>No vivarium run yet. /vivarium-runs &lt;out&gt; follows one.</Text>
    }
    return (
      <Box flexDirection="column">
        {list.map(run => (
          <Box key={run.out} flexDirection="column" marginBottom={1}>
            <Text bold color={colour(run)}>
              {run.id} {summary(run)}
            </Text>
            {run.status === 'building' && run.building && <Text dimColor>{run.building}</Text>}
            {run.phases.map(phase => (
              <Text dimColor={phase.state === 'pending' || phase.state === 'deselected'}>
                {mark(phase.state)} {phase.name}
              </Text>
            ))}
            {run.error && <Text color="red">{run.error.split('\n')[0]}</Text>}
            {!isFinal(run) && (
              <Box flexDirection="row">
                {run.status === 'paused' && (
                  <Button key={`${run.out}:continue`} hotkey="c" variant="primary" onPress={() => void send($, run, 'continue')}>
                    Continue
                  </Button>
                )}
                <Button key={`${run.out}:stop`} hotkey="s" dimColor onPress={() => void send($, run, 'stop')}>
                  Stop
                </Button>
              </Box>
            )}
          </Box>
        ))}
      </Box>
    )
  })
}

const colour = (run: Run): string | undefined =>
  ({ passed: 'green', failed: 'red', gone: 'red', paused: 'yellow', stopped: 'yellow' })[run.status as string]

const mark = (state: string): string =>
  ({ passed: '✓', failed: '✗', running: '▸', skipped: '-', interrupted: '!', deselected: ' ' })[state] ?? '·'
