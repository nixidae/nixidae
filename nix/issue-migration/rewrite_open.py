#!/usr/bin/env python3
"""Rewrite issue references in the bodies and comments of open nixidae/nixidae issues.

  rewrite_open.py            dry run: print each change
  rewrite_open.py --apply    edit through GraphQL; log to rewrite-open.log

`<owner>/<source>#N`, `<source>#N` and old issue URLs become `#M` through
map.jsonl; `nixidae/nixidae#M` becomes `#M`. Bodies are read fresh per issue.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SOURCES = "easykubenix|ghanix|havsorm|huggorm|nanopynix|nixkube|pydd|pynixd|vivarium"
NEW = {(r["repo"], r["old"]): r["new"] for r in map(json.loads, (HERE / "map.jsonl").read_text().splitlines())}
APPLY = "--apply" in sys.argv

URL = re.compile(rf"https://github\.com/(?:nixidae|Lillecarl)/({SOURCES})/issues/(\d+)\b(?!#)")
QUALIFIED = re.compile(rf"\b(?:nixidae|Lillecarl)/({SOURCES})#(\d+)\b")
SHORT = re.compile(rf"(?<![/\w])({SOURCES})#(\d+)\b")
SELF = re.compile(r"\bnixidae/nixidae#(\d+)\b")

Q = """
query($n:Int!){repository(owner:"nixidae",name:"nixidae"){issue(number:$n){
  id body comments(first:100){nodes{id body}}}}}"""


def gh_json(*args: str) -> dict:
    return json.loads(subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout)


def mapped(m: re.Match[str]) -> str:
    new = NEW.get((m[1], int(m[2])))
    return f"#{new}" if new else m[0]


def rewrite(text: str) -> str:
    text = URL.sub(lambda m: f"https://github.com/nixidae/nixidae/issues/{NEW[(m[1], int(m[2]))]}"
                   if (m[1], int(m[2])) in NEW else m[0], text)
    text = QUALIFIED.sub(mapped, text)
    text = SHORT.sub(mapped, text)
    return SELF.sub(r"#\1", text)


def update(kind: str, node: str, body: str) -> None:
    mutation = {"issue": "updateIssue", "comment": "updateIssueComment"}[kind]
    query = f"mutation($i:ID!,$b:String!){{{mutation}(input:{{id:$i,body:$b}}){{clientMutationId}}}}"
    subprocess.run(["gh", "api", "graphql", "-f", f"query={query}", "-f", f"i={node}", "-f", f"b={body}"],
                   capture_output=True, check=True)


def log_from_snapshot(path: str) -> None:
    """Write rewrite-open.log from a snapshot.py output taken before the rewrite."""
    with (HERE / "rewrite-open.log").open("w") as log:
        for i in json.loads(Path(path).read_text()):
            if i["state"] != "OPEN":
                continue
            for kind, node, body in [("issue", i["id"], i["body"])] + [("comment", c["id"], c["body"])
                                                                      for c in i["comments"]["nodes"]]:
                if rewrite(body) != body:
                    log.write(json.dumps({"issue": i["number"], "kind": kind, "id": node, "before": body}) + "\n")


def main() -> None:
    if "--log-from-snapshot" in sys.argv:
        log_from_snapshot(sys.argv[sys.argv.index("--log-from-snapshot") + 1])
        return
    numbers = [i["number"] for i in gh_json("issue", "list", "-R", "nixidae/nixidae", "--state", "open",
                                             "--limit", "2000", "--json", "number")]
    edits = 0
    with (HERE / "rewrite-open.log").open("a") as log:
        for n in sorted(numbers):
            issue = gh_json("api", "graphql", "-f", f"query={Q}", "-F", f"n={n}")["data"]["repository"]["issue"]
            targets = [("issue", issue["id"], issue["body"])] + [("comment", c["id"], c["body"])
                                                                  for c in issue["comments"]["nodes"]]
            for kind, node, body in targets:
                new = rewrite(body)
                if new == body:
                    continue
                edits += 1
                print(f"#{n} {kind} {node}", flush=True)
                if APPLY:
                    update(kind, node, new)
                    log.write(json.dumps({"issue": n, "kind": kind, "id": node, "before": body}) + "\n")
                    log.flush()
    print(f"{edits} edits{'' if APPLY else ' (dry run)'}")


main()
