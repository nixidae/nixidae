Measured at `57514eff` on 2026-10-13, x86_64, 16 cores; the median of 3 runs.

| | cli | huggorm | drv | out |
| --- | ---: | ---: | ---: | ---: |
| planner closure (MiB) | 322.7 | 509.5 | 305.1 | 508.5 |
| lines of the mode | 328 | 195 | 601 | 560 |
| a build that does nothing (s) | 1.25 | 1.41 | 1.40 | 1.40 |

Planner time, in seconds:

| project | packages | cli | huggorm | drv | out |
| --- | ---: | ---: | ---: | ---: | ---: |
| application | 4 | 3.27 | 2.51 | 2.80 | 2.40 |
| binary-app | 1 | 2.53 | 2.61 | 2.48 | 2.70 |
| binary-nixpkgs | 1 | 2.56 | 2.46 | 2.32 | 2.75 |
| git-app | 0 | 2.53 | 2.66 | 2.59 | 2.69 |
| local-index | 2 | 2.50 | 3.01 | 2.52 | 2.65 |
| native-app | 3 | 2.86 | 2.75 | 2.43 | 2.67 |
| native-host | 3 | 2.78 | 2.67 | 2.32 | 2.69 |
| numpy-sdist | 1 | 2.38 | 2.72 | 2.56 | 3.21 |
| packaged-app | 4 | 3.30 | 2.53 | 2.81 | 2.52 |
| packaged-app-bin | 4 | 3.01 | 2.44 | 2.67 | 2.56 |
| packaged-sdist | 4 | 2.93 | 3.03 | 3.23 | 2.66 |
| pure-app | 4 | 2.54 | 2.90 | 2.65 | 2.47 |
| root-derivation | 4 | 2.79 | 2.86 | 2.42 | 2.71 |
| vendored-app | 2 | 2.72 | 2.54 | 2.37 | 2.47 |
| workspace-all | 4 | 3.15 | 3.35 | 3.25 | 2.83 |
| workspace-core | 0 | 2.99 | 3.09 | 3.04 | 3.09 |
| workspace-core-sdist | 4 | 3.15 | 2.73 | 2.76 | 2.73 |
| zmq-sdist | 1 | 2.60 | 2.88 | 2.46 | 2.80 |
| zmq-sdist-transitive | 1 | 2.46 | 2.67 | 2.60 | 3.02 |
