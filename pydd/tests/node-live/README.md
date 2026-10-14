# Live npm projects

Real projects from GitHub, at the releases that nixpkgs builds:
`nix run --file . test-node-live`. Each builds in three modes (`cli`,
`huggorm`, `recursive`), and the modes must give one result.

| project | what it covers | needed | changed in pydd |
| --- | --- | --- | --- |
| http-server 14.1.1 | a plain CommonJS program | the commit after the release, which writes a lock of version 3 | nothing |
| nodemon 3.1.14 | a large development tree, from semantic-release | nothing | nothing |
| npm-check-updates 23.0.2 | TypeScript, bundled by vite, declarations by api-extractor | `program = "ncu"` | the project builds outside `node_modules` |
| json-server 1.0.0-beta.3 | TypeScript, compiled by tsc, an ES module | nothing | nothing |

**http-server's release has a lock of version 1**, which names no install
paths. The planner refuses it. nixpkgs applies the next commit of the
project, which writes the lock again, and so does this test.

**nodemon from source says version `0.0.0-development`.** semantic-release
writes the version at publication. So `nodemon --version` names no
release, and the check reads `--help`. nixpkgs' build is the same.

**npm-check-updates failed while the root built in
`$out/lib/node_modules`.** TypeScript and api-extractor skip every file
under a `node_modules` directory, and so skipped the project's own
sources. The root now builds in the build directory, and copies the
result.
