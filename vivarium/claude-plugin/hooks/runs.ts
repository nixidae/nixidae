import type { PhaseRow, Run, RunStatus } from '../types'

/** One line of `vivarium monitor --json`. */
export type Item = {
  event?: string
  run?: string
  text?: string
  phase?: string
  state?: string
  reason?: string
  passed?: string
  stopped?: string
}

const FINAL: ReadonlySet<RunStatus> = new Set(['passed', 'failed', 'stopped', 'gone'])

export const isFinal = (run: Run): boolean => FINAL.has(run.status)

export const basename = (path: string): string => path.replace(/\/+$/, '').split('/').pop() ?? path

export const absolute = (path: string, cwd: string): string =>
  path.startsWith('/') ? path : `${cwd.replace(/\/+$/, '')}/${path.replace(/^\.\//, '')}`

export const fresh = (out: string, cli: string[], phases: string[] = []): Run => ({
  out,
  id: basename(out),
  cli,
  status: 'waiting',
  phases: phases.map(name => ({ name, state: 'pending' })),
})

export const monitorArgv = (run: Run): string[] => [...run.cli, 'monitor', run.out, '--json', '--through-pauses']

export const ctlArgv = (run: Run, op: string): string[] => [...run.cli, 'ctl', '--out', run.out, op]

const withPhase = (phases: PhaseRow[], name: string, state: string): PhaseRow[] =>
  phases.some(p => p.name === name)
    ? phases.map(p => (p.name === name ? { ...p, state } : p))
    : [...phases, { name, state }]

/** The phases `run.json` names, in its order, with the states seen so far;
 * a phase seen and not named (a reload removed it) stays, last. */
export const merged = (named: string[], seen: PhaseRow[]): PhaseRow[] => [
  ...named.map(name => seen.find(p => p.name === name) ?? { name, state: 'pending' }),
  ...seen.filter(p => !named.includes(p.name)),
]

/** `run passed: boot passed, incr failed` to each phase's state. */
export const verdictPhases = (text: string): PhaseRow[] => {
  const at = text.indexOf(': ')
  if (at < 0) return []
  return text
    .slice(at + 2)
    .split(', ')
    .map(part => part.trim().split(' '))
    .filter(words => words.length === 2)
    .map(([name, state]) => ({ name: name as string, state: state as string }))
}

/** The run after one monitor item. */
export const apply = (run: Run, item: Item): Run => {
  const text = item.text ?? ''
  const next: Run = { ...run, last: text || run.last }
  const moving = isFinal(run) ? run.status : 'running'
  switch (item.event) {
    case 'building':
      return { ...next, status: isFinal(run) ? run.status : 'building', building: text }
    case 'live':
      return { ...run, status: run.status === 'waiting' || run.status === 'building' ? 'running' : run.status }
    case 'progress':
      if (!item.phase) return next
      return {
        ...next,
        status: run.status === 'paused' ? 'paused' : moving,
        phases: withPhase(run.phases, item.phase, item.state === 'started' ? 'running' : (item.state ?? 'running')),
      }
    case 'failed':
      return {
        ...next,
        status: run.status === 'paused' ? 'paused' : moving,
        phases: item.phase ? withPhase(run.phases, item.phase, 'failed') : run.phases,
        error: text,
      }
    case 'paused':
      return { ...next, status: 'paused', reason: item.reason ?? text }
    case 'resumed':
      return { ...next, status: moving, reason: undefined }
    case 'finished': {
      const status: RunStatus = item.stopped === 'true' ? 'stopped' : item.passed === 'true' ? 'passed' : 'failed'
      const given = verdictPhases(text)
      return { ...next, status, reason: undefined, phases: given.length > 0 ? given : run.phases }
    }
    case 'exited':
      return { ...next, status: isFinal(run) ? run.status : 'gone', error: isFinal(run) ? run.error : text }
    default:
      return next
  }
}

/** Whole lines from a stream of pieces: each call takes the pieces so far
 * and the buffer, and gives the lines and what is left. */
export const lines = (buffer: string, piece: string): { lines: string[]; rest: string } => {
  const all = (buffer + piece).split('\n')
  const rest = all.pop() ?? ''
  return { lines: all.filter(line => line.trim() !== ''), rest }
}

export const parseItem = (line: string): Item | undefined => parseObject(line) as Item | undefined

/** The `--out` of a `vivarium run ... --out O` command line, if it is one.
 * A shell variable in it is not followed: a hook cannot expand it. */
export const outOfCommand = (command: string): string | undefined => {
  if (!/\bvivarium\s+run\b/.test(command)) return undefined
  const found = /--out[= ](?:"([^"]+)"|'([^']+)'|(\S+))/.exec(command)
  const out = found ? (found[1] ?? found[2] ?? found[3]) : undefined
  return out === undefined || out.includes('$') ? undefined : out
}

/** The `out` and the CLI argv from the reply of the MCP server's `start`. */
export const startReply = (text: string): { out: string; cli: string[] } | undefined => {
  const reply = parseObject(text)
  if (!reply || typeof reply.out !== 'string' || typeof reply.monitor_all !== 'string') return undefined
  const argv = reply.monitor_all.split(/\s+/).filter(Boolean)
  const at = argv.indexOf('monitor')
  return at > 0 ? { out: reply.out, cli: argv.slice(0, at) } : undefined
}

const parseObject = (text: string): Record<string, unknown> | undefined => {
  try {
    const value: unknown = JSON.parse(text)
    return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

export const summary = (run: Run): string => {
  const running = run.phases.find(p => p.state === 'running')
  switch (run.status) {
    case 'waiting':
      return 'waiting for the run'
    case 'building':
      return 'building'
    case 'running':
      return running ? `running ${running.name}` : 'running'
    case 'paused':
      return `paused ${run.reason ?? ''}`.trim()
    default:
      return run.status
  }
}
