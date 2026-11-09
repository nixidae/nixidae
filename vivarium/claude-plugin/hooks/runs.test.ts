import { expect, test } from 'claude-code/testing'

import { apply, fresh, lines, merged, outOfCommand, parseItem, progress, startReply, summary } from './runs'

const RUN = 'vivarium-internal-incr-q9okrapd'

test('a run through a pause to its verdict', () => {
  const items = [
    { run: RUN, event: 'building', text: "building '/nix/store/x-typecheck.drv'" },
    { event: 'live' },
    { run: RUN, event: 'progress', phase: 'boot', state: 'started', text: 'phase boot started' },
    { run: RUN, event: 'progress', phase: 'boot', state: 'passed', text: 'phase boot passed in 0s' },
    { run: RUN, event: 'paused', reason: 'before incr', text: 'paused before incr; the guests are up' },
    { run: RUN, event: 'resumed', reason: 'before incr', text: 'resumed, paused before incr' },
    { run: RUN, event: 'progress', phase: 'incr', state: 'started', text: 'phase incr started' },
    { run: RUN, event: 'finished', passed: 'true', text: 'run passed: boot passed, incr passed' },
  ]
  let run = fresh('/tmp/' + RUN, ['vivarium'], ['boot', 'incr'])
  const seen: string[] = []
  for (const item of items) {
    run = apply(run, item)
    seen.push(summary(run))
  }
  expect(seen).toEqual([
    'building',
    'running',
    'running boot',
    'running',
    'paused before incr',
    'running',
    'running incr',
    'passed',
  ])
  expect(run.phases).toEqual([
    { name: 'boot', state: 'passed' },
    { name: 'incr', state: 'passed' },
  ])
})

test('a run that dies before it starts is gone, a finished one stays finished', () => {
  const dead = apply(fresh('/o', ['vivarium']), { event: 'exited', text: 'no run: error: undefined variable' })
  expect(dead.status).toBe('gone')
  expect(dead.error).toBe('no run: error: undefined variable')
  const done = apply(fresh('/o', ['vivarium']), { event: 'finished', passed: 'false', text: 'run failed: a failed' })
  expect(apply(done, { event: 'exited', text: 'x' }).status).toBe('failed')
})

test('a stopped run reads stopped', () => {
  const run = apply(fresh('/o', ['vivarium']), { event: 'finished', passed: 'false', stopped: 'true', text: 'run stopped (ctl stop): a passed' })
  expect(run.status).toBe('stopped')
})

test('pieces become whole lines', () => {
  const first = lines('', '{"event":"li')
  expect(first.lines).toEqual([])
  const second = lines(first.rest, 've"}\n{"event":"x"}\n')
  expect(second.lines.map(parseItem)).toEqual([{ event: 'live' }, { event: 'x' }])
  expect(second.rest).toBe('')
})

test('the out of a vivarium run command', () => {
  expect(outOfCommand('vivarium run internal.incr --out "$OUT" --until-pause')).toBeUndefined()
  expect(outOfCommand("vivarium run x --out '/a b' --until-pause")).toBe('/a b')
  expect(outOfCommand('cd x && vivarium run lan --out ./o -- -k y')).toBe('./o')
  expect(outOfCommand('vivarium run lan --out=/abs')).toBe('/abs')
  expect(outOfCommand('vivarium ctl --out ./o state')).toBeUndefined()
})

test('the reply of the MCP start tool', () => {
  const text = JSON.stringify({
    run: RUN,
    out: '/tmp/' + RUN,
    monitor: '/nix/store/p/bin/python3.14 -m vivarium.cli monitor /tmp/r --quiet',
    monitor_all: '/nix/store/p/bin/python3.14 -m vivarium.cli monitor /tmp/r --through-pauses',
  })
  expect(startReply(text)).toEqual({ out: '/tmp/' + RUN, cli: ['/nix/store/p/bin/python3.14', '-m', 'vivarium.cli'] })
  expect(startReply('not json')).toBeUndefined()
})

test('the phases run.json names keep their order and the states seen', () => {
  expect(merged(['boot', 'incr'], [{ name: 'boot', state: 'passed' }])).toEqual([
    { name: 'boot', state: 'passed' },
    { name: 'incr', state: 'pending' },
  ])
})

test('a failed phase marks the phase and keeps its error until the pause', () => {
  let run = fresh('/o', ['vivarium'], ['boot', 'check'])
  run = apply(run, { event: 'progress', phase: 'check', state: 'started', text: 'phase check started' })
  run = apply(run, { event: 'failed', phase: 'check', text: 'phase check failed: AssertionError: 1 != 2' })
  expect(run.phases[1]).toEqual({ name: 'check', state: 'failed' })
  expect(run.error).toBe('phase check failed: AssertionError: 1 != 2')
  run = apply(run, { event: 'paused', reason: 'after check failed', text: 'paused after check failed' })
  expect(summary(run)).toBe('paused after check failed')
})

test('build counts from the plan to the run', () => {
  const tally = (built: string, running: string, downloaded: string) => ({
    built,
    builds: '3',
    running,
    downloaded,
    downloads: '40',
  })
  let run = fresh('/o', ['vivarium'], ['boot'])
  run = apply(run, { event: 'builds', text: 'planned', ...tally('0', '0', '0') })
  expect(summary(run)).toBe('building: builds 0/3 · downloads 0/40')
  run = apply(run, { event: 'builds', text: '/nix/store/s', ...tally('0', '0', '12') })
  run = apply(run, { event: 'building', text: "building 'a.drv' (1/3 built)", ...tally('1', '1', '40') })
  expect(summary(run)).toBe('building: builds 1/3, 1 running · downloads 40/40')
  run = apply(run, { event: 'progress', phase: 'boot', state: 'started', text: 'phase boot started' })
  expect(summary(run)).toBe('running boot')
  expect(run.builds?.built).toBe(1)
})

test('progress names only the kinds with work', () => {
  expect(progress({ built: 2, builds: 2, running: 0, downloaded: 0, downloads: 0 })).toBe('builds 2/2')
  expect(progress({ built: 0, builds: 0, running: 0, downloaded: 5, downloads: 9 })).toBe('downloads 5/9')
})
