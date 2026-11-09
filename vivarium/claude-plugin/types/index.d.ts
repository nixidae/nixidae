export type RunStatus =
  | 'waiting'
  | 'building'
  | 'running'
  | 'paused'
  | 'passed'
  | 'failed'
  | 'stopped'
  | 'gone'

export type PhaseRow = { name: string; state: string }

export type Run = {
  /** The run's `--out`, absolute: what every command takes. */
  out: string
  /** The id the monitor names it by: the basename of `out`. */
  id: string
  /** The argv before a subcommand: `ctl` and `monitor` follow it. */
  cli: string[]
  status: RunStatus
  phases: PhaseRow[]
  reason?: string
  building?: string
  last?: string
  error?: string
}

declare module 'claude-code' {
  interface PluginState {
    'vivarium-runs': { runs: Shaped<Run[]> }
  }
}
