# nixidae

One repository, twelve sources (`nix/members.nix`): eleven projects and `dotgithub`, the organisation's `.github` repository. josh exports each source to its own repository on every push to `main`, with the source's original commit hashes. nixidae/nixidae#4.

This is the only shared instruction file. Shared rules first, then one section per source under **Sources**. A source with no section has no rules of its own.

## Rules

- Commit here only. A push straight to an exported repository is not brought back, and the next export refuses to overwrite it.
- One tracker: nixidae/nixidae. Every issue of the exported repositories moved here on 2026-10-19. Every exported repository has issues and pull requests turned off; a new source's repository is created that way (`gh api -X PATCH repos/nixidae/<source> -F has_issues=false -F has_pull_requests=false`). An old `nixidae/<source>#N` URL redirects, but `gh issue view` on it fails: use `gh api repos/nixidae/<source>/issues/N --jq .html_url`, or `nix/issue-migration/map.jsonl`.
- Labels: one `source: <name>` per issue (`source: monorepo` for josh, lock-subtrees, shared CI and pins), plus `upstream` or `needs decision` when they apply. The kind is the issue type (Bug, Feature, Task), not a label.
- `user-initiated`: Agent never starts it; User does. Sub-items the issue marks for agents are open (#866: nanopynix to huggorm).
- `off-main`: work on its own bookmark in its own workspace; nothing lands on `main` (#868: fix).
- Tests run here only. An exported repository runs its Pages workflow and nothing else.
- kr8s, containerd and flake-compatish are not sources. Each is a pin in `flake.lock`; an ignored checkout at `./<name>` replaces the pin. kr8s and containerd patches must stand as upstream pull requests.
- No gate runs kr8s' own suite (it needs a pip venv and a kind cluster). Run it before landing a change to the kr8s pin. Its 568 tests are a floor: a change that passed them still broke server-side apply.
- Shared tool configuration lives in `nix/shared/`. Edit it there, then `nix run --file . shared-config-update`; never edit `<source>/nix/shared/` or `<source>/nix/sources.nix` by hand (the template is `nix/shared/sources.nix.in`).
- Nix is formatted with nixfmt: `nix run --file . nixfmt-update`. `checks.nixfmt` fails on an unformatted tracked `.nix` file.
- Nix is linted with statix (`nix/statix.toml`): `checks.statix`. `nix run --file . statix-fix` applies its fixes, and drops comments inside parentheses it removes: diff the comments after it.
- A source's test workflows are `ciTestWorkflows` in its `default.nix`. Edit those, then `nix run --file . ci-workflow-update`. Never edit `.github/workflows/<source>-*.yml` by hand.
- A sibling import passes `sources`: `import sources.<name> { inherit pkgs sources; }`. Without it the sibling reads its own `nix/sources.nix` from its store copy and fetches the nixidae commit its lock names: a second, older set of sources.
- No source is a flake build. `<source>/nix/sources.nix` asks nixidae where every dependency lives; `nix build --file <source> <attr>` is the way in. A source's `flake.nix` is a surface that calls `default.nix`; lock-subtrees writes its `flake.lock`.
- The pins live in the root `flake.lock`. A dependency moves when that file moves, so CI and a local run build the same derivation.
- jj: a new file reaches a build only after a jj command has snapshotted it (it is then intent-to-add in git's index, which `fetchGit` reads).
- jj workspaces: add one with `jj workspace add --colocate` (jj >= 0.46), so it gets a git worktree that `fetchGit` reads. A workspace without `.git` refuses to evaluate; `jj git colocation enable` converts it.

## Landing

```sh
jj commit -m ...                    # the work, in as many commits as it needs
nix run --file . lock-subtrees      # commits the locks if a source moved
jj bookmark set main -r @-
jj git push --bookmark main
```

lock-subtrees commits the locks with jj, so landing needs jj; `--check` also runs in a plain git checkout. The push starts the `josh export` run: `lock-subtrees --check` and `josh-export --verify --push`. Landing ends at the push; do not wait for that run. josh runs in CI only: its vendored libgit2 refuses the `relativeworktrees` extension that a colocated jj workspace sets.

## Gates

```sh
nix build --file . checks.ciWorkflows checks.callFlake checks.licenses checks.pythonpath checks.mypyIni checks.sharedConfig checks.nixfmt checks.statix
nix run --file . lock-subtrees -- --check
```

Each source's own gates are in its section below.

## Personal instructions

This file holds what is true for everyone. One person's workflow goes in `.claude/claudemd/<name>.md`. Claude Code loads it through a gitignored `CLAUDE.local.md` at the root that holds one line, `@.claude/claudemd/<name>.md`. Each checkout and each jj workspace needs its own copy of that line.

## Shared conventions

### Dev shells

A source's tools (pytest, ruff, zuban, nixfmt) run in that source's dev shell, its `shell.nix`. Use the dev shell's tool, not one off PATH: it is the version the gate runs.

### Commits

- Commit each piece when it is complete and its tests pass. One commit says one thing. A subject that needs "and" is two commits.
- `Closes #N` on its own line at the end of the body when the commit completes nixidae/nixidae#N; one line per issue. `Refs #N` for a part. GitHub closes the issue when the commit reaches `main`.
- Never close an issue by hand ahead of the commit. A decision that ends without code gets a closing comment that says what was decided.
- Prose (docs, comments, commit messages, error messages): ASD-STE100. One topic per sentence, active voice, simple tense, one word per meaning. nanopynix commit subjects carry a Conventional Commits prefix (`fix(tests):`); ASD-STE100 applies after it.

### Issues

- `gh issue create --repo nixidae/nixidae --label "source: <name>"`. Never git-bug: its `refs/bugs/*` are fetched and pushed by nothing, so nobody sees the issue.
- Inside a source's files, write `nixidae/nixidae#N`: a bare `#N` links nowhere in the exported repository. A bare `#N` in a source's older files and history is that source's old number; `nix/issue-migration/map.jsonl` maps it.
- An issue, PR or comment body an agent writes ends with `---` and `Assisted-By: <model>`.
- Work named and not done gets an issue. Comment measurements on the issue when taken. Record what turned out wrong, and what refuted it.

### Python

- `from __future__ import annotations`; never a string type hint.
- Imports at the top. A lazy import only to break a cycle; first try moving the shared types to a neutral module.
- Order: `__future__`, stdlib, third-party, first-party, `if TYPE_CHECKING:` (type-only imports, last; nanopynix bans it), module constants, code.
- Re-export: `from .m import Name as Name`, one multi-line block.
- No `assert` outside `tests/`: `if ...: raise ...`. For the type checker, a local alias or `if x is None: raise`.
- No `except Exception: pass`. Log the unexpected; `contextlib.suppress(...)` with a comment for the expected.
- A suppression names its rule and gives its reason: `# type: ignore[rule]  # reason` (zuban and mypy refuse text after `]` without a second `#`), `# noqa: RULE -- reason`.
- `pathlib.Path` for anything that is not wire protocol; to `str` as late as possible. `time.monotonic()` for a duration; `anyio.current_time()` for a value a cancel scope is measured against.
- Concurrency is anyio and structured. `nix/shared/ruff-anyio.toml` bans the asyncio task API (`create_task`, `gather`, `wait_for`, `asyncio.Lock`, `asyncio.sleep`, ...) and names the replacement; `kr8s.asyncio` is a module path, not one of them. Every task runs in a task group some caller owns. Timeouts: `anyio.fail_after` / `move_on_after`. A site that stays unstructured carries a comment saying why.
- Teardown under cancellation: `with anyio.CancelScope(shield=True):` around the whole teardown, bounded by `move_on_after`. Unshielded, `await process.wait()` in a `finally` never finishes and the child survives.
- Drive Nix through nanopynix or huggorm, never the `nix` CLI from Python: `Session()`, `session.store(uri=...)`, `store.is_valid_path`, `store.query_path_info`, `store.copy_closure`, `session.settings()` (ekn's `eval.py` `push_closure_to_store` and `storecheck.py` show the shape). Kubernetes through kr8s, never raw HTTP. A binding keeps distinct answers distinct (`is_valid_path` tells absent from unreachable; an exit code does not). Grep the dependency list before concluding there is no binding.

### Comments

Default to none. A comment carries what the code cannot: why this way when an obvious alternative is wrong, a measured fact, a trap. Not: restating the line, narrating structure, history, or a log message repeated. Present tense.

### Test failures

- Assume Agent's change caused or exposed a failure. "Pre-existing", "unrelated", "flaky", "out of scope" need proof first: revert and show it still fails, run it on a clean baseline, or find a CI record from before the work. Without one: "I have not proven this is unrelated; debugging on the assumption my change caused it."
- Procedure: re-run the exact command; read the failure; review the diff; isolate the smallest failing test; smallest fix for the root cause; re-run it, then the wider set. Remove temporary logging.
- Keep the full pytest output; never judge from a filtered tail.
- Never change a test to match broken behaviour. Change it only when the intended behaviour changed, the old expectation is shown obsolete, and the reason is written down. No weakened assertion, skip or deleted coverage without that.
- A failure report names the command, the test, the message, whether it reproduced after the change, and why the fix reaches the root cause.
- Lost track of file state against the VCS, or a tool output looks corrupt: stop, re-read from disk. Read a whole file before rewriting it.

# Sources

## easykubenix

### Gates and commands

Run from `easykubenix/`, in its dev shell: `pytest`, `zuban mypy`, `ruff check --fix`, `ruff check --config ruff-strict.toml`, `ruff format --check`, `nixfmt` over every tracked `.nix`.

- `nix build --file ./checks.nix all`: the doc-example gates plus `ekn-sandbox`, `nixfmt`, `validation-e2e`, `bootstrap-validation-e2e`, `tofu-render`, `tofu-schema`. What CI builds. `nix build --file`, `nix run --file` and `nix-shell --run` must always work; nothing is gated behind a flake command.
- `validation-e2e`: a real etcd and kube-apiserver on 127.0.0.1 apply the full manifest set in the sandbox (nixidae/nixidae#358), ~10 s. Debug run: `nix run --file ./nix packages.validationScript`, with `validation.debug = true` in the module to see the control plane's output.
- `bootstrap-validation-e2e`: the same harness over `docs/examples/bootstrap`'s nested instance (ArgoCD's CRDs, then the `Application` that needs them). Separate because `kubernetes.generated` excludes a GitOps target's submodule objects by design.
- `tofu-render`: renders a `class = "tf"` unit, diffs `config.tf.json` against a literal, runs `tofu init` and `tofu validate` in the sandbox. No network there, so `tofu.providers` must pin the provider through the store. See `nix/tofu`.
- `tofu-schema`: `tofu.schemaCheck` (`ekn.tofuschema`) held to `tofu validate` case by case (`nix/tofu/schema.nix`, cases in `nix/tofu/differential.py`).
- Outside `all`: `tofu-registry` (registry providers, fetches a ~424M index; run it when touching `easykubenix/lib/tofuRegistry.nix`) and `kubeapply` (a kubeadm cluster under UML runs `ekn _applyManifest`: CRD Established, workloads run, what a prune deletes; minutes of CPU; run it when touching `ekn.apply` or kappy; see `nix/kubeapply`).
- `checks.nixfmt` filters the tree to `*.nix` minus dot-directories, `result` links and `__pycache__`, so a new directory is covered with no list to extend.

### Rules

- `ekn/` is easykubenix's alone. nanopynix has no ekn dependency; ekn uses nanopynix through its public API only. Never import `nanopynix._*`.
- ekn does not opt into beartype; only pydantic resolves annotations at run time. So `TC001`/`TC002` are on, and `ruff-strict.toml` sets `runtime-evaluated-base-classes = ["pydantic.BaseModel"]`; without it those rules move a live model field type into `if TYPE_CHECKING:` and break imports.
- A command that opens an API against a real cluster subclasses `cli.FencedCommand` and calls `self._fence(api, cfg)` right after `kr8s.asyncio.api(...)`, above anything that writes. It checks `ekn.clusterUid`; `clusterfence.require` is the rule. `_applyManifest` and `validate` are exempt: their apiserver is fresh in a Nix build.
- Process teardown: `ekn.validation.terminate_process` is the shape.
- `rich.traceback.install(show_locals=True)` in CLI entry points. `structlog`, `_log = structlog.get_logger()` at module level.
- Background work belongs to a task group, and a group cancels every sibling when a child raises: per-request work that may fail needs a wrapper that keeps `Exception` inside.
- Do not declare an option for what ekn can read at run time. Exception when what it can read is not what is meant: the deploying machine is not the fetching one. `ekn.assertCached` is a `listOf str`, not a bool over the deployer's substituters, because CI's `nix.conf` names three caches and a node carries one. What describes ekn itself, ekn reads; what describes another machine is data, in an option.

### Profiling

- `EKN_PROFILE=pyinstrument`: wall clock, async-aware; the only one that says what a command waits for. For `kubeapply`, `deploy`, anything network-bound. `EKN_PROFILE_FILE` (default `ekn.profile.html`), `EKN_PROFILE_INTERVAL` (default 0.001; raise it for a long run).
- `EKN_PROFILE=1`: cProfile, call counts for CPU-bound Python. One frame for every wait (`epoll.poll` 8.108 s of an 11.039 s render).
- `NIX_COUNT_CALLS=1 NIX_SHOW_STATS=1 NIX_SHOW_STATS_PATH=x.json nix eval ...`: evaluator call counts; deterministic under load.
- pyinstrument is not in the release build. A consumer shell that wants to profile takes `easykubenix.passthru.eknDevEnv`, not `.ekn`.
- Start the profiler inside the event loop; around `anyio.run` it charged 72% of a `clusterdiff` to `selectors.py:select`. `main` wraps `command.run()`; `tests/test_profile.py` holds both arms.
- A call count is not a time: removing ~30% of a render's calls bought 3.1% of the stage. State a saving in seconds or say "calls".
- No Python profiler sees the evaluator (a nanopynix worker process): ~2.8 s of an 8.2 s stage is attributed to nothing. Measure on a warm store, or an IFD build reads as evaluation.

## huggorm

### Purpose

huggorm binds Nix to Python and generates the binding from a declaration: one file per Nix class decides the C++ binding, the type stub, the wire policy and the async class (in process or on a server).

The destination is an evaluation service: one `EvalState` serving many connections over days, clients that detach and reclaim the same warm state, no re-evaluation of unchanged input, every non-store file an evaluation touched under a watch, registered expressions evaluated eagerly in the background. The RPC layer exists so the evaluator outlives the process that asked. nixidae/nixidae#553 holds the lifecycle contract. Audiences, unranked: the maintainer's Nix tooling, editor and direnv-style workflows, and people who want Nix from Python.

Milestone: a second client claims a live `EvalState`, and re-evaluating unchanged input does no re-evaluation. Watched files and eager evaluation come after.

### Rules

- Breaking compatibility is encouraged when it improves the codebase (maintainer, 2026-09-05). No name, wire field, constructor or signature is a reason to keep a worse shape. Say what broke in the commit. Still ask before an action irreversible outside the repo, or a choice between materially different work.
- Picking work, in order:
  0. Architecture first (maintainer, 2026-10-05). Every stage reads the typed model (`huggorm_gen/ir.py`, nixidae/nixidae#566); the runtime reads `Wire` descriptors the build emits. A rule lives on the model once; a type never crosses a stage as a string. Standing mandate: fix poor architecture anywhere; change the declaration DSL when a simpler spelling removes emitter work; add decl types where C++ needs a home (`Path = Annotated[pathlib.Path, Cxx("std::filesystem::path", ...), Async(...)]`), so no emitter keeps a mapping table. Every change to emitted code is deliberate: diff outputs against the parent commit's; a restructure shows an empty diff; an improvement is its own commit.
  1. A defect that can corrupt or silently drop a value. The named failure mode is the silent skip: an emitter skips what it does not recognise, or erases what a later reader needs (`pyerrors.module` stripped `cxx` lines off the cached tree, so a later translator caught nothing). Found so far: nixidae/nixidae#610, #612, #615, #619, #624, #625, #627.
  2. What the destination needs next.
  3. An outstanding issue that blocks nothing.
  The board is the open `source: huggorm` issues, minus `user-initiated` and `off-main`. nixidae/nixidae#867 is the standing codegen review and never closes: a review pass is a comment there, each finding its own issue.
- Correctness against Nix: read Nix's source when unsure; never guess. A binding is never more permissive than the C++ it binds. Prove a gate by breaking it.
- No hand-written C++ mapping. `packages/huggorm-decl/src/huggorm_decl/decl/` is the source; the nanobind C++, wire policy, sync and async APIs, stubs and enums are emitted. A mapping is any C++ saying "this Python name means that C++ call" (accessor, constructor, type test, enum table, conversion): banned however small; teach the declaration instead. A helper is infrastructure generated code calls (a GC root over a foreign collector, thread registration, an owner whose member order is the fact): allowed with no ask; the commit says what it does and why a declaration cannot carry it. Tests: who calls it (a Python name resolving to it makes it a mapping), and could a declaration say it. Best helpers are what the emitter binds to: a table, a symbol, a slot. Ask when it is a design choice (ownership, threading, lifetime).
- Maintainability: one source, many outputs; derive, do not restate. When correctness and no-mapping conflict, correctness wins and the conflict is an issue.
- anyio everywhere (maintainer, 2026-09-05), task groups wherever a task starts. One measured exception: the emitted runtime's thread bridge (`huggorm_gen/payload/_runtime.py`) keeps `loop.run_in_executor`, because `anyio.to_thread.run_sync` runs on a shared churning pool that cannot name a thread, and an `EvalState` is affine to one thread; `_refuse_foreign` compares executor identity (nixidae/nixidae#572). Anything new that needs a thread asks first.
- Speed is not a goal yet. Fix a pathology when found; never trade a derived mapping for a hand-written fast one.

### Building

- Cheap first: `nix build --file . internal.bindings-src` (seconds, 124 KB) before `packages.huggorm-bindings` (minutes, 3.3 MB). Prove most emitter changes by diffing the emitted C++ against the previous store path; compile only when the C++ changed shape. Exception: what nanobind resolves at compile or run time; a missing type_caster emits and compiles fine and fails a gate later (nixidae/nixidae#604). Dead outputs filled the disk before (nixidae/nixidae#599, #605).
- The build's reports (`hand-written C++ in cpp/: N lines`, the orphan list) are in the derivation log, not the terminal; a cached build prints nothing. `nix build --file . internal.bindings-src --no-link --json`, then `nix log <drvPath>`. `--json` with `--print-out-paths` prints two documents. Claimed absent three times and present every time (nixidae/nixidae#628).
- `nix/versions.nix` lists the Nix lanes and every patch; each patch file starts with its reason and measurement.
- Scratch files go in `huggorm/.scratchpad` (gitignored).
- Unsure of a dependency's API: read its source, `nix build --no-link --print-out-paths --file . internal.pkgs.<package>.src`.
- History: `tasks/NNN` in a commit message is old huggorm#N, for N up to 105; nixidae/nixidae#643 holds the old board.

### How the codegen works

Declarations are in `packages/huggorm-decl/src/huggorm_decl/decl/`, one file per Nix class, named after its header. `read.py` reads each twice: it imports it (Python resolves any `NIX_VERSION` branch) and parses it with `ast.parse` for what the import drops. No body runs, so C++ in a body is dead text the reader lifts out. `ir.py` resolves that into the typed model; the emitters write the nanobind C++, stubs, enum modules and every Python surface from it.

## nanopynix

### Gates and commands

Run from `nanopynix/`, in its dev shell.

- `timeout 1200 pytest`: every suite (`testpaths` in `pytest.ini`). `pytest tests` is the self-checks and static gates alone.
- Coverage: `timeout 1200 pytest --cov --cov-report=term-missing --cov-report=`. A spawned interpreter takes part through `nanopynix_testing`'s `sitecustomize.py`. pytest-agent writes the ~88-line table to `.pytest-agent/runs-NNNN/reports.txt` and prints only the total; read that file, or pass `--agent-max-summary-lines=0`. Never re-run with another reporter.
- `zuban mypy --python-executable "$(command -v python)"` from each `mypy.ini`'s directory (`checks.types` runs them all). `ruff check --fix`. `ruff check --config ruff-strict.toml --fix`: zero findings; a new one is the change's. Never `--unsafe-fixes`.
- As CI runs them, in the sandbox: `nix build --file . --no-link --keep-going checks.lint checks.lint-strict checks.format checks.types checks.shell checks.pytest-agent checks.test-support checks.libpynix checks.pynix-isolated checks.completions`.
- A CI suite by hand: `nix build --file . internal.ciSteps.<lane> --out-link result`, then `./result/bin/nanopynix-ci suite`.
- A different Nix version: `nix develop --file . lanes.<version>.shell`. `supportedNixFloor` in `default.nix` is the oldest supported Nix (2.34); CI builds 2.34, 2.35 and `git`.
- A Nix source: `nix build --file . pkgs.nixVersions.nix_2_34.src --no-link --print-out-paths`.
- An engine change reaches nothing until the dev shell is rebuilt: it holds a built `huggorm-bindings`. A test that fails locally and passes in CI: check `python -c "import huggorm; print('pure-eval' in huggorm.list_settings())"` (stale: 83 settings, five failures; current: 113).

### CI

- Test workflows: `ci/workflows/{commit,schedule,experiment}.nix`, rendered by nixidae's `ci-workflow-update`. The Pages workflow, `ci/workflows/on_pages.nix`, renders to `.github/workflows/on_pages.yml` through `python ci/render.py`; `tests/test_ci_workflows.py` compares and rewrites it on every run.
- A `run:` body is one line with no `${{ ... }}`; `tests/meta/test_ci_step_policy.py` enforces both. The body is a `writeShellApplication` in `ci/steps.nix` (shellcheck reads it, it runs locally against one Nix, `runtimeInputs` names each tool), called as `nix build --file . "$CI_STEP" --out-link result` then `./result/bin/nanopynix-ci suite`. Values reach a step through `env:`, never an expression in a command.

### Rules

- `pynix` runs on huggorm's public API. A convention a machine can check goes in `tests/meta/`, not in prose.
- No `if TYPE_CHECKING:`: pynix installs deferimport, which makes imports lazy, and the beartype hook (`DEFERIMPORT=eager`) needs every annotation name real. `ruff-strict.toml` bans `typing.TYPE_CHECKING`.
- In an async function, never a blocking call, tests included: `anyio.open_process` for `subprocess`/`os.system` (it pipes stdin, stdout and stderr by default; pass `None` to inherit), `anyio.Path` for path I/O (`.read_text()`, `.exists()`, `.mkdir()`). Pure path manipulation stays `pathlib.Path`. `asyncio.get_running_loop()`, never `get_event_loop()`.
- A command writes its result to stdout and everything else to stderr; stderr stays quiet at the default verbosity (what `nix` prints and no more); a pynix event is a warning, an error, or waits for `--verbosity talkative`. `pynix/tests/test_stderr.py`. In Nix, "stderr" means `nix::Logger` events; they reach pynix as huggorm log records. No separate stderr pipe for them.
- No version branch in library code to keep an old Nix alive; gate the test and name the upstream defect and issue. No gate the floor already answers (`minimum="2.32"` under a 2.34 floor; nixidae/nixidae#161 removed nine). Raise the floor when an old version costs more than it reports; #161 is the model.
- Apache-2.0, every project; each carries a `LICENSE` copy because `license-files` cannot name a path above the source root. No project links Nix: huggorm's bindings do. `huggorm-bindings` ships Nix's `src/nix/get-env.sh`, copied from the Nix source it links. `tests/meta/test_licensing.py`.
- `libpynix/` (nixidae/nixidae#261) depends on `argcomplete` and nothing else: one package for every Nix version. `libpynix.command` is generic; `libpynix.nix_options` names Nix and reads none of its options (reading a target means an evaluator; that cost 101 ms, nixidae/nixidae#158). `pynix._settings` keeps `configured=`; `opt` refuses `required=True` with `configured=True`. `docs/libpynix/index.md` documents it. A completer for the three options waits on a budget (nixidae/nixidae#262). Tests: `timeout 60 pytest libpynix`.
- `pynix-lsp/` (nixidae/nixidae#136) carries `pygls`, `lsprotocol` and `jsonschema`; 349 of 966 modules of `import pynix` were theirs. It imports `pynix._nix_syntax` and `pynix._completion`; pynix knows nothing of it. One name, `pynix-lsp`; no `lsp` subcommand. Tests: `timeout 300 pytest pynix-lsp/tests`.
- The code-review roadmap is nixidae/nixidae#55.

### Tests

pytest-agent starts in an agent harness (it detects `CLAUDECODE`) and writes each test's detail to `.pytest-agent/runs-NNNN/`. It refuses a pipe from its stdout into `head`, `tail`, `grep`, `sed`, `awk` or a renamed grep (exit 2); `tee` and `wc -l` are allowed, and a listing run (`--collect-only`, `--fixtures`) is exempt. Read the failed test's detail file; the last line of `.pytest-agent/history.jsonl` names the run. Load the `pytest-agent` skill (`pytest-agent/SKILL.md`; `.claude/skills/pytest-agent/SKILL.md` links to it, edit the target) for `digest` (start here; one `.pytest-agent/` per rootdir), `watch --run <label>`, `history '<test>'`, `rerun`, `--agent-label`, and `agent_notes` / `from pytest_agent import note`.

Choose a test's directory by what it looks at:

| directory | holds |
| --- | --- |
| `tests/meta/` | self-checks: the repository read as text and structure, milliseconds, no Nix, store, subprocess or network |
| `tests/gates/` | CI's static gates run as tools: `xfail(strict=False)` always, skip when the tool is absent; `nix/checks.nix` is the authority |
| `tests/scripts/` | `scripts/` shell run against a bare repo under `tmp_path`: git, a temp dir, nothing else |
| `tests/support/` | scanners for the meta tests; no tests |
| `pynix/tests/` | the CLI, one command per test; `support/` holds the store scenario and helpers |
| `pynix-lsp/tests/` | the language server |
| `pynix/completions/tests/` | completions of the installed `pynix` in fish, bash and zsh on a pty; not in `testpaths`, run by `checks.completions` |
| `libpynix/tests/`, `test-support/tests/` | the command-line layer; the generic helpers |

- Each project has its own `pytest.ini` and rootdir. Plugins register with `-p` in `pytest.ini`, never `pytest_plugins` in a conftest (legal only at the top level).
- Shared layer: `test-support/src/test_support/` (no Nix concept), `nanopynix-testing` (the beartype hook every suite loads first). Nix fixtures: `huggorm.testing.environment`, `huggorm.testing.settings`. A helper leaves `tests/support/` when a second project needs it. Losing the beartype hook is silent; `pynix/tests/test_beartype_instrumentation.py` notices.
- A helper gets its own test only when its bug makes no other test fail (`hang_report.py`, `subprocess_output.py`).
- Meta test shape, copy `test_suppression_grammar.py`: a guard that the scanner found source (the CI runner works in a store copy), scanner unit tests both ways, then the conformance test naming file and line. Walk the AST, not text. Reuse `iter_python_files`.
- A derived set compared with a declared literal carries the reason beside each entry and asserts both directions, so a stale entry fails.
- A module docstring names a nearby rule the test does not check (ASD-STE100, whether a reason is true).

## nixkube

nixkube injects Nix stores into pods: CSI ephemeral volumes requested through `volumeAttributes` (`storePath`, `nixExpr`, `flakeRef`), and NRI hooks driven by pod annotations. A node DaemonSet runs both; a cache StatefulSet coordinates builds and substitution; builder pods are optional. CSI driver names: `nixkube`, and `nix.csi.store` behind `cfg.node.compat`.

### Gates and commands

Run from `nixkube/`.

- Before committing (CI's `check` job): `nix run --file . internal.treefmt` and `nix build --file ./checks.nix all`. treefmt from the nixidae root formats the whole monorepo.
- Fast loop: `nix develop --file shell.nix --command python -m pytest pkgs/nixkube/tests pkgs/nri-wait/tests pkgs/pynixd-nixkube/tests pkgs/grpclib-ttrpc/tests pkgs/grpclib-nri/tests`.
- Root-only tests: `nix build --file . internal.nixosTests.bind-farm internal.nixosTests.csi-mount-rec internal.nixosTests.farm-builder --no-link`.
- Manifests: `nix build --file . internal.kubenixApply.manifestJSONFile`. Both architectures (needs builders): `nix build --file . internal.push --no-link`. Push to cachix and the registry: `nix run --file . internal.push`.
- The test workflow is `ciTestWorkflows.tests` (`ci/workflows/tests.nix`), rendered to `.github/workflows/nixkube-tests.yml` by nixidae's `ci-workflow-update`.

### CI

`nixkube-tests.yml`: `check` (types, eval assertions, `arch-audit`, treefmt); `build-amd64`/`build-arm64` (environments and the nix image, pushed to cachix, native runners only); `build-manifests` (every named store path fetchable, multi-arch index); `test-qemu` (the `nix/uml` node test as a VM on the runner); `test-qemu-ci` and `test-qemu-ci-cache` (`kubenixCI2`/`kubenixCI1` through `kubenixDeploy` on a kubeadm guest, with and without pynixd, running `kubenix/ci/test-workloads.nix`; `ci/test-jobs.nix` says which Jobs must finish; by hand `nix run --file . internal.ciTest.driver -- --out ./out`); `release`. Publishing runs from `main` and `nixkube/v<version>` tags only (every source shares one tag namespace). Every test job uploads `--out`.

### Layout

- `environments/{cache,node}`: dinix environments for x86_64 and aarch64, run by dinit in a minimal container. Cache: `shared-setup` → `nix-daemon` → `cache-daemon` → `cache-logger` → `cache`. Node: `shared-setup` → `nix-daemon` → `csi-gc` → `csi-daemon` → `csi-logger` → `csi`. `config-reconciler` syncs SSH keys and Nix config from mounted volumes.
- `kubenix/`: easykubenix manifests. `options.nix` builds `cachePackage` and `nodePackage`; `cache.nix`, `daemonset.nix`, `builder.nix`; `config.nix` sets the cache substituter `ssh-ng://nix@pynixd?trusted=1`; `./keys/*.pub` become authorized keys.
- The cache watches pods labelled `app.kubernetes.io/component=node` and writes `/etc/machines` with builder DNS names (`pod.name.nixkube-builders.namespace.svc.cluster.local`).
- `pkgs/nixkube/src/`: `cli.py` runs `csi/server.py` (grpclib, `csi-proto-python`) and `nri/server.py` (ttrpc) together; logging from `/etc/nix/logging.json`; `ENABLE_COMPAT_DRIVER`, `NRI_ENABLED`. Version in `pkgs/nixkube/pyproject.toml`. `niximage.nix` builds the initContainers' Nix image.

### Rules

- structlog: a plain event string plus kwargs, never an f-string or `%` in the event (`logger.info("fetched_packages", count=n)`). `logger.exception` in an `except` that re-raises; `logger.warning(..., exc_info=True)` in one that swallows; `logger.error(..., exc_info=t.exception())` in a callback. Bind per-request context: `structlog.get_logger("nixkube.nri.buildtask").bind(container_id=...)`.
- Code stays approachable to beginners. Comments that earn a line: design choices, protocol requirements (CSI, Kubernetes, Nix), error-handling philosophy (see `NodeUnpublishVolume`), performance reasons, footguns.
- Integration tests over unit tests: the code orchestrates Nix, rsync and mount. The vivarium guest tests (`ciTest`, `ciTestCache`, `umlTest`) run a real kubeadm node, locally as in CI. Unit tests for pure logic only.

## pynixd

### Gates and commands

Run from `pynixd/`.

```sh
nix build --file . checks.format checks.lint checks.types --no-link
nix run --file . internal.tests.guest.driver -- --out ./out
```

- `tests.guest` runs the five suites (unit, protocol, parity, functional, differential) at once, each in a vivarium container guest: 115 s for the session, 2 s of it booting. One suite: `-- --out ./out --only prepare --only unit`; `--break-on-failure` keeps the guest up, `vivarium ctl --out ./out exec ...` reaches in. Timings: unit 13 s, protocol 3 s, parity 17 s, differential 22 s, functional 109 s. Run it in the background; with the vivarium MCP, `start` with `attr = "internal.tests.guest"` and watch the `monitor` command.
- Never the suites with host pytest: it reads the host's config (`use-cgroups` killed every managed nix-daemon on one host; functional and differential errored, wirelog tests waited out 30 s). If anyway, one pytest process at a time: functional tests share store paths, sockets and `/tmp/pynixd-stores`.
- `checks.*` are gates; `nix run --file . internal.fix` is the rewriter, never a gate.
- Evidence: `a=$(nix eval --raw --file . internal.tests.guest.attempt)` keeps a failed run: `phases.json`, `junit.xml`, `events.jsonl`, `artifacts/<suite>/`, each guest's process census. `jq -c 'select(.kind == "case" and .data.outcome == "failed") | .text' $a/events.jsonl`.

### Guest tests

Phases: `prepare`, then `unit`, `protocol` and `parity` at once, then `leaks` (runs after a failure too; fails on a daemon that outlived its suite, which found nixidae/nixidae#426). `tests/guest/` holds `prepare.py` and `leaks.py`; `tests/derivations/guest/` declares them, and each suite through vivarium's `suites` recipe. `-- -k name` selects inside every suite (pair it with `--only`); `PYNIXD_NO_DAEMON=1` is a knob. Evidence goes to `/artifacts` (`$PYNIXD_TEST_LOG_DIR`); `/tmp` dies with the guest. Suites run as `tester` through the guest's daemon.

### Architecture

Protocol support: clients, builder stores and local stores >= 1.35 (`MINIMUM_PROTOCOL`: Lix and nixbuild.net speak 1.35; untested, guests run Nix >= 2.34, `supportedNixFloor`). No legacy below it. pynixd advertises 1.38 and translates.

1. `OpRequest.handle(proxy)`: decodes from the client, delegates `await proxy.local_store.execute(request)`. Streaming ops (`NarFromPath`, `AddToStore`) override it.
2. `OpRequest.execute(store, client=None, suppress_last=False)`: the recipe and its fast paths (SQLite, caches); else `await store.call(self, client=client, suppress_last=suppress_last)`.
3. `Store.execute(request, ...)`: calls `request.execute(self, ...)`.
4. `Store.call(request, ...)`: wire protocol, pooling, handshake.

Builds: a global `BuildQueue` and a DAG-aware `Scheduler`. `BuildPaths` and `BuildPathsWithResults` split into `BuildDerivation` requests, each in a task that survives client disconnect; outputs are pulled into the `LocalStore`.

### Rules

- Every buffered response carries a `StderrBuffer`. With a `ClientConn` passed to `execute()`, logs stream as read, never buffered: buffering a substituting `QueryValidPaths` grew pynixd to 10.4 GB (nixidae/nixidae#507). Keep messages only with `keep_logs=True`, only the kinds needed. The terminating `LogError` is never streamed.
- Sub-operations suppress intermediate `STDERR_LAST` (`suppress_last`). A no-op or cached operation injects a `StderrNext` such as `"pynixd: IsValidPath (SQLite hit)"`.
- Trust: `Role.ADMIN` trusted, `Role.USER` untrusted (`pynixd/trust.py`, nixidae/nixidae#525). Upstream connections are root's, so pynixd applies each untrusted-user rule in the op's `handle`, with Nix's message at Nix's point. An error before `startWork` closes the session (`ClosingError`); one after does not. Never a silent success for a refused op. `execute` always performs the upstream operation.
- HTTP cache: a NAR transfer that fails after `200 OK` closes the connection; no full buffering.
- Types: `if TYPE_CHECKING:` for cross-module imports, runtime imports never after it. Concurrency: `Server` owns a task group (`async with Server(...)`), `Server._spawn(fn, *args)` starts into it, `Server.start()` alone cannot run loops. A loop catches what it expects; an unexpected error ends the daemon and systemd restarts it. Unstructured sites are rare: `loop.call_later` for write lingering, asyncssh. `time.monotonic()`, not `loop.time()`. Concurrent results in a test: `tests/_conftest/all_of.py`.
- `GLOSSARY.md` defines Nix and pynixd terms; extend it with each new pattern or term.

### Divergence markers

Matching `nix-daemon` byte for byte is a measure, not the goal. Every divergence gets a verdict, and "pynixd is right" is one.

Standing rule (maintainer, 2026-11-09): where pynixd can do better than Nix, it does, and tags it with one of the markers below. Copying a Nix weakness is never the default. Parity holds where Nix is right. An improvement the maintainer defers becomes an issue (nixidae/nixidae#1257: store health remembered across clients, where Nix forgets it per worker).

| Verdict | Marker |
| --- | --- |
| Nix is wrong; pynixd copies it, or answers correctly | `NIX-DEFECT (#23):` (nixidae/nixidae#412) |
| Nix is right; pynixd answers differently on purpose | `NIX-DEVIATION (#27):` (nixidae/nixidae#416) |

- `NIX-DEFECT`: the Nix mechanism with file and line in back quotes; what it gets wrong; what pynixd could do instead; why pynixd still copies it or how it deviates, naming the defect's report.
- `NIX-DEVIATION`: the Nix mechanism with file and line; what pynixd does; why it is worth the cost; what to measure to reverse it.
- A divergence that makes pynixd better stays. It is complete with a measurement, a marker, a pytest, and an `EXEMPTIONS` entry in `wirelog/diff.py`. Scope the exemption: one on `response.will_build` covers seven differences of nixidae/nixidae#415 and hides an eighth with another cause (nixidae/nixidae#241).
- No "Nix-correct mode" against a "correct-result mode": the parity run proves one. A flag is scoped to one measured entry.
- `tests/meta/test_nix_defect_markers.py` checks each marker names its issue and a Nix file.
- A correction from the Nix functional suite is complete when a pytest here asserts the same thing, its docstring naming the suite's file and line. Copy: `tests/unit/test_build_log_replay.py` (`build.sh:167`), `test_derived_path.py` (`build.sh:91`), `test_build_paths_goals.py` (`build.sh:8`), `test_keep_going_and_max_jobs.py` (`build.sh:247`, `:269`), `test_dispatch_order.py` (`build.sh:167`, `:176`), `test_ensure_child_logs.py` (`build.sh:167`), `tests/parity/test_wire_parity.py` (`build.sh:91`).

### Tests

- `tests/functional/` end-to-end, `tests/benchmark/`, `tests/legacy/` (never modify).
- Test stores use `STORE_PREFIX` (`/tmp/pynixd-stores`, `tests/conftest.py`); clean with `rmtree_robust_glob(f"{STORE_PREFIX}*")` in an autouse fixture. Few tests touch the root store.
- `run_captured(cmd, ...)` returns `(rc, stdout, stderr)`; `run_logged` streams through structlog; both set `NIX_SSHOPTS`. The Nix binary: `env.str("NIX_BIN", "nix")`.
- Simple, explicit tests; commands as plain lists. Never `@pytest.mark.asyncio`: the conftest wraps every `async def` test in `anyio.fail_after`. The configured 120 s timeout per test is enough.

## vivarium

Commands run from `vivarium/`. `README.md` explains the pieces; `docs/howto.md` is the user guide and stays true when a command, flag or printed line changes. `docs/design/runner.md` is the design.

### Rules

- Every test runs with plain Nix, in the sandbox and out of it: `nix build --file . <test>` and `nix run --file . <test>.driver`. The runner (`vivarium-runner`, in each spec) never evaluates. Anything else may use vivarium's own programs, as long as a Nix-compatible CLI wraps them.
- The `vivarium` CLI is built on huggorm (`pkgs/vivarium-eval`). Impure runs evaluate in-process through it, so it can evaluate a run again, switch a guest and reload a live run. This is the base for agent troubleshooting workflows on impure runs. Prefer a huggorm solution over one that shells out to `nix`.
- Python, not bash. A check body is a script in `tests/checks/` on the shared `harness.py`, and the Nix builder only calls it. A bash body that a change touches moves to Python in that change. Bash needs a strong reason, such as the busybox `/init` of a guest with no Python.

### Structure

- `mkTest` is the one function: guests plus named phases ordered in Nix, with `.driver`, `.driverDebug`, `.driverInteractive`. `fromNixosTest` (`nixos-test.nix`) maps a nixos-test spec onto it.
- `pkgs/vivarium-runner` is the mechanism (guests, backends, agent channel) with no opinion on sequence; `pkgs/vivarium` owns the sequence (a `Session` driven a step at a time; `vivarium run` is one linear drive). Teardown is never automatic, and nothing per-run is a module global.
- Logic: pure functions in `pkgs/vivarium/vivarium/phases.py`, tested without a guest. Effects: `session.py`. `checks.phase-rules` caught the driver ignoring a correct phase-skip answer.
- The repo is a library. A test script belongs in the project it tests (`(import sources.vivarium { inherit pkgs sources; }).lib.mkTest { ... phases.check = { script = ./tests/vivarium/check.py; after = [ "boot" ]; }; }`). `tests/` here is for vivarium's own facilities. A helper a second script wants is a method on `Machine` in `vivarium_runner`. Type-check scripts with `vivarium.typeCheck { name; scripts; }` (zuban strict; an unannotated script does not build).
- Workflows: `ci/workflows.nix`, rendered by nixidae's `ci-workflow-update`.

### Running

- By name: `vivarium run checks.pytest-phase --out ./out -- -k hostname` (or `nix run --file . packages.vivarium -- run ...`); `vivarium phases checks.recipes`. The `vivarium` CLI is built on huggorm (`pkgs/vivarium-eval`), evaluates impurely, builds `.driver` and steps into a check's `.session`. Not a CI check. Tests: `nix build --file . packages.vivarium-eval.tests`.
- Do not iterate by editing a phase and re-running. Pause and send Python: `nix run --file . mine.driver -- --out ./o --break check &`, then `vivarium ctl --out ./o` with `state`, `exec '<python>'` (top-level await; `session`, `vms`, each guest, `anyio` in scope; names persist), `exec - < file`, `inject ./scratch.py`, `pytest ./tests/x -- -k y`, `run <phase>`, `continue`, `stop` (guests down, exit 5, verdict `stopped`). `state` names the pause reason and each failed phase's error; `ctl --json` prints the reply as JSON. `exec`, `inject`, `state` and `stop` also work while phases run; `pytest`, `run` and `continue` need a pause. By-hand pytest cases are `case` events with `data.by_hand`, outside `status` and `junit.xml`. The socket is `<out>/control.sock` (0600), absent in a sandboxed check (`--no-control`). `vivarium/control.py`; `checks.breakpoint`.
- `--kernel PATH` boots your own kernel (`make ARCH=um` linux, or a bzImage with virtio), by hand only; `checks.kernel-override`. Incremental kernel: `pynix build --file . --attr internal.umlKernelCcache --namespaced --sandbox-path /ccache=$HOME/.cache/uml-ccache` (122 s cold, 31 s after a one-line change). Traps: ccache on the config derivation breaks `.config`; `buildFlags` carry their own `CC=`; `env` does not export under structured attrs.
- Always tee a build to a log; boot output is long. `grep '\[test\]'` shows what a test did; every console line is prefixed `[<machine>]`.

### The MCP server

`.mcp.json` registers `vivarium-mcp` as `vivarium`: `start`, `state`, `exec`, `inject`, `run_pytest`, `run_phase`, `resume`, `switch`, `screenshot`, `stop`, `events`, `runs`.

- Two `.mcp.json`: the root one (any session in nixidae) and `vivarium/.mcp.json`. Both build `vivarium-mcp` from the working tree; `/mcp` reconnect picks up an edit. A user-scope `vivarium` server (croshome) loses to them by name.
- `packages.vivarium`: `vivarium` (the huggorm CLI: run, phases, switch, ctl, monitor; on PATH in the pynixd, nixkube and easykubenix dev shells), `vivarium-mcp`, and the agent skill `vivarium/skill/vivarium/SKILL.md` at `share/skills/vivarium/vivarium`. Keep the skill true when a command, flag or printed line changes, as `docs/howto.md`.
- `screenshot` returns image content (FastMCP `structured_output=False`). Acting on the screen: `exec` with `Machine`'s screen methods (`vivarium.display`, QEMU only; `checks.screen`).
- `switch` runs `vivarium switch`: host eval of the session plus one module on one node, `Machine.add_closure`, `switch_to(<path>)`. A container guest takes late paths through `crun_launch attach`.
- A run started by `start` is a child with `--break-on-failure`: MCP's stdio is the server's stdout.
- Channel events (`failed|paused|finished|exited`) reach Claude only when started with `claude --dangerously-load-development-channels server:vivarium`. Without it, `start` returns `monitor`, a `vivarium monitor --quiet` command line: prints a pause, a failure and the verdict, exits 4 at a pause (re-arm after `resume`). `monitor_all` drops `--quiet` and adds `--through-pauses`.
- `vivarium monitor <out|run id>` waits for a run that has not started (`--wait`, 1800 s), replays, then follows (`--json` for JSONL); exits 0 passed, 1 failed, 2 no verdict, 3 stream ended first or no run, 4 paused (`--through-pauses` goes on), 5 stopped. Without a live `monitor.sock` it follows `events.jsonl`. `vivarium/monitor.py`.
- Every run with a control socket prints `commands()` at start and each pause. `checks.copy-commands`; `internal.mcp-check` drives the server over raw JSON-RPC.

### Evidence

- A test derivation never fails: `<test>.attempt` writes `status`, `<test>` reads it. A failed run is a kept output: `ls "$(nix eval --raw --file . checks.artifacts.attempt)"` (`status log report.json artifacts/`); a pass links the same into `result/`.
- `events.jsonl`: every event carries `machine`, `phase`, `seconds`; ask with `jq`, not a regex. `console/<guest>.log` per guest; `log` unfiltered.
- Each guest's journal streams as `journal` events (`data.unit`, `data.identifier`, `data.priority`) through `/artifacts/journal.jsonl`; hostfs and virtiofs are write-through, so a SIGKILLed guest keeps what it logged (`stream`).
- `/artifacts` in the guest is a host directory (`vms.artifacts / "<node>"`); put junit, logs and `ps` snapshots there, not through `succeed`.
- `await vm.processes()`, `await vm.count_processes("nix-daemon")` read `/proc` through the agent.
- Never `print` in the runner: `Session.emit` is the one way in. A phase script may print. `-vv` shows consoles; a failed phase replays the last 20 lines of each guest.
- `report.json`: `total_seconds`, `boot_seconds`, `waiting_seconds`, `.slowest`, `.by_command`. A `wait` step contains its `rpc` steps; never sum them. `Machine._ask` is the one choke point. Never guess at slowness or add timing prints.

### Phases

- A pytest phase: `phases.cases = { pytest.tests = ./tests/guest; after = [ "boot" ]; }`. Guests are fixtures by name, `vms` is all; tests may be `async def`. pytest runs in a worker thread, async goes back through a portal. An async generator fixture's setup and teardown are two portal calls: no task group across its `yield`. `vivarium/pytest_plugin.py`; `pytest-phase`.
- A suite inside a guest writes JUnit to `/artifacts/junit/*.xml`; each test becomes a `case` event. `vms.phase` names the phase; `vms.shared` survives across phases (`checks.guest-suites`).
- `phases.<name>.nodes = [ ... ]` declares a phase's guests (empty is all). Phases whose guests do not overlap run at once. `vms` holds only the declared guests. Attribution is by guest, prints through a context variable; `settle`, the JUnit import and the console replay take the phase's guests, never all. A pytest phase holds every guest. A breakpoint or failure stops new phases. `vivarium run --serial`. `phases.ready`/`launchable`, `cli._schedule`; `checks.parallel`.

### Backends

- `vivarium.backend`: `uml`, `qemu`, `krun` or `container`; a node may set its own. Every test carries `.uml`, `.qemu`, `.krun` and `.container`; never add a second attribute. A test script and a node config never know the backend; differences go in `modules/qemu.nix` (krun's under `krun`) or `vivarium_runner/backend.py`. Mixed runs: `checks.mixed`, by hand.
- After touching `machine.py`, `net.py` or `forward.py`: `nix build .#lan .#lan.qemu`. Segment wiring also runs `segments` (`--option extra-sandbox-paths /dev/net`). `.#lan.qemu` needs `/dev/kvm`.
- QEMU guests get `-cpu host` minus `vmx`/`svm`; `vivarium.nestedVirtualization = true` passes it (`nested`, by hand).
- `container` (nixidae/nixidae#530): crun, no kernel. By hand: a user namespace, 65536 subordinate ids with working `newuidmap`/`newgidmap`, a delegable cgroup; `container.probe()` names what is missing; `container-probe` / `container-probe-tun` fail in seconds in the sandbox. Sandboxed (`checks.container`): a daemon with `uid-range` (CI: ghanix `nix.install.uidRange`), `/dev/net` in `extra-sandbox-paths` (ghanix `nix.install.devNet`). The store is an rbind; the launcher mounts the cgroup2 crun wants (`_ensure_cgroup2`); seccomp refuses setuid, so suid-sgid-wrappers are skipped. Without a writable cgroup the launcher runs under `systemd-run --user --scope -p Delegate=yes`.
- `modules/container.nix`: `boot.isContainer`, an overlay over the host's `/nix/store` (all of `/nix` fails with EINVAL unprivileged). LAN: `crun_launch tap` (`container-lan`). Uplink: pasta as `vec0`; `/sys` read-only so udevd stays off. Agent on `unix:/run/host/agent/sock`. `crun_launch` relays the console pty; EIO is systemd reopening the console.
- Lifetime chain: runner → launcher → crun, pasta, tap (`die_with_parent`) → init (`setpriv --pdeathsig KILL`). SIGKILL of the runner leaves nothing; keep every link.

### Network

- `vivarium.forward` is fixed before boot: passt binds every socket while parsing arguments and has no control socket. `auto` is pasta-only.
- In `forward.py`: a spec of only exclusions (`127.0.0.2/~32768-60999`) puts passt in weak mode (unbindable ports skipped); any base range makes failures fatal. Overlapping specs are fatal, so the privileged block is excluded and re-added with an offset. Check against real passt: `passt --foreground -s /tmp/p.sock <-t args>`, `ss -tlnH | grep -c 127.0.0.2`; the socket path must be short.
- `nix build .#iperf` lies under host load (1.9 vs 4.4 Gbit/s) and outside a fresh netns (`net.unix.max_dgram_qlen`); compare back to back, under `unshare -rn`.

### Kubernetes test

`.#k8s` is three guests and ~5 GB RAM: for CI. Iterate with `.#check-k8s-images .#check-k8s-config` (seconds) and `.#containerd` (one guest, ~2 min); CI runs `test-k8s` after `test-containerd`. Build without running: `nix build .#k8s.spec`.

- Images are symlinks into `/nix/store`; containerd's `base_runtime_spec` bind-mounts the store. The pod sandbox lacks that spec, so `pause` is built with its closure.
- A layered image carries no references: `system.extraDependencies` in `modules/k8s.nix` puts etcd on the node. Add an image through `imageSpecs`; a hand-written symlink shows up as `executable file not found in $PATH`.
- Container kernel options: `containerConfig` in `pkgs/uml-kernel/default.nix`; `ignoreConfigErrors` drops unknown or unmet options silently. Check: `grep -E '^CONFIG_(NF_|IP_NF_|VETH|BRIDGE)' "$(nix build --no-link --print-out-paths .#umlKernel)/config"`.

### Traps

- A guest agent failure surfaces as an exception from `vm.execute`; the traceback is in `await vm.journal("vivarium-agent")`.
- `vivarium.memory` below ~192M OOM-kills the agent mid-test, which looks like a hang.
- `vivarium.memory` is a ceiling: guest RAM is a sparse file and freed blocks are punched out. `vm.host_memory_kib()` is the host cost. At 1024M, boot / after reading the closure / after `drop_caches`: UML 149M, 553M, 148M; QEMU 349M, 782M, 404M.
- `vm.shrink("256M")` / `vm.grow(...)` squeeze the guest; `drop_caches` first and read `vm.meminfo()`.
- Writes to `/nix/store` in a guest land in a tmpfs overlay and vanish at poweroff, by design.
