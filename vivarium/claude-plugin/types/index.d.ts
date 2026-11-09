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

/** Nix's work before the run: each store path once, planned or seen. */
export type BuildCounts = {
  built: number
  builds: number
  running: number
  downloaded: number
  downloads: number
}

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
  builds?: BuildCounts
  last?: string
  error?: string
}

declare module 'claude-code' {
  interface PluginState {
    'vivarium-runs': { runs: Shaped<Run[]> }
  }
}
