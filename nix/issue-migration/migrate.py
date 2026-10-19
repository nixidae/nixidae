#!/usr/bin/env python3
"""Transfer every issue of the exported repos into nixidae/nixidae, oldest first.

  migrate.py            dump to dump.json, print the count
  migrate.py --apply    transfer, label, type; one map.jsonl line per transfer
  migrate.py --fixup    label/type/sub-issue links from map.jsonl and dump.json

Resumable: a transferred issue no longer lists under its old repo.
Transfer drops sub-issue links (measured), so --fixup restores them.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

OWNER = "nixidae"
TARGET = "nixidae"
REPO = f"{OWNER}/{TARGET}"
SOURCES = ["easykubenix", "ghanix", "havsorm", "huggorm", "nanopynix", "nixkube", "pydd", "pynixd", "vivarium"]
HERE = Path(__file__).parent
MAP = HERE / "map.jsonl"
DUMP = HERE / "dump.json"

LIST = """
query($owner:String!,$name:String!,$after:String){
  repository(owner:$owner,name:$name){
    issues(first:100,after:$after,orderBy:{field:CREATED_AT,direction:ASC}){
      pageInfo{hasNextPage endCursor}
      nodes{id number title createdAt state issueType{name}
            labels(first:30){nodes{name}} assignees(first:10){nodes{login}}
            parent{number repository{nameWithOwner}}}
    }
  }
}"""

TRANSFER = """
mutation($i:ID!,$r:ID!){
  transferIssue(input:{issueId:$i,repositoryId:$r,createLabelsIfMissing:false}){
    issue{number issueType{name}}
  }
}"""

LIMIT_HINTS = ("rate limit", "abuse", "502", "503", "timeout", "was submitted too quickly")


def gh(*args: str) -> str:
    attempt = 0
    while True:
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        if any(h in p.stderr.lower() for h in LIMIT_HINTS):
            attempt += 1
            wait = min(60 * attempt, 900)
            print(f"limited, sleeping {wait}s: {p.stderr.strip()[:200]}", flush=True)
            time.sleep(wait)
            continue
        raise RuntimeError(f"gh {' '.join(args[:4])}: {p.stderr}")


def graphql(query: str, **vars: str | None) -> dict:
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in vars.items():
        if v is not None:
            args += ["-f", f"{k}={v}"]
    return json.loads(gh(*args))["data"]


def issues(repo: str) -> list[dict]:
    out, after = [], None
    while True:
        page = graphql(LIST, owner=OWNER, name=repo, after=after)["repository"]["issues"]
        for n in page["nodes"]:
            n["repo"] = repo
            out.append(n)
        if not page["pageInfo"]["hasNextPage"]:
            return out
        after = page["pageInfo"]["endCursor"]


def fallback_type(labels: list[str]) -> str:
    if "bug" in labels:
        return "Bug"
    if "enhancement" in labels:
        return "Feature"
    return "Task"


def finish(rec: dict) -> None:
    gh("issue", "edit", str(rec["new"]), "-R", REPO, "--add-label", f"source: {rec['repo']}")
    if not rec["newType"]:
        gh("api", "-X", "PATCH", f"repos/{REPO}/issues/{rec['new']}", "-f", f"type={fallback_type(rec['labels'])}")


def apply(todo: list[dict]) -> None:
    target_id = json.loads(gh("api", f"repos/{REPO}"))["node_id"]
    with MAP.open("a") as f:
        for i in todo:
            new = graphql(TRANSFER, i=i["id"], r=target_id)["transferIssue"]["issue"]
            rec = {"repo": i["repo"], "old": i["number"], "new": new["number"], "state": i["state"],
                   "type": (i["issueType"] or {}).get("name"), "newType": (new["issueType"] or {}).get("name"),
                   "labels": [l["name"] for l in i["labels"]["nodes"]]}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            try:
                finish(rec)
            except RuntimeError as e:
                print(f"finish failed for #{new['number']}, --fixup will retry: {e}", flush=True)
            print(f"{i['repo']}#{i['number']} -> #{new['number']}", flush=True)
            time.sleep(3)


def fixup() -> None:
    recs = [json.loads(l) for l in MAP.read_text().splitlines()]
    labelled = {i["number"] for i in json.loads(gh("issue", "list", "-R", REPO, "--state", "all", "--limit", "2000",
                                                    "--json", "number,labels,issueType"))
                if any(l["name"].startswith("source: ") for l in i["labels"]) and i["issueType"]}
    for rec in recs:
        if rec["new"] not in labelled:
            print(f"fixup labels/type #{rec['new']}", flush=True)
            finish(rec)
    new_of = {(f"{OWNER}/{r['repo']}", r["old"]): r["new"] for r in recs}
    for i in json.loads(DUMP.read_text()):
        if not i["parent"]:
            continue
        child = new_of.get((f"{OWNER}/{i['repo']}", i["number"]))
        pkey = (i["parent"]["repository"]["nameWithOwner"], i["parent"]["number"])
        parent = new_of.get(pkey) or (pkey[1] if pkey[0] == REPO else None)
        if child is None or parent is None:
            print(f"cannot relink {i['repo']}#{i['number']} under {pkey}", flush=True)
            continue
        cur = json.loads(gh("api", f"repos/{REPO}/issues/{child}"))
        child_id = cur["id"]
        try:
            gh("api", "-X", "POST", f"repos/{REPO}/issues/{parent}/sub_issues", "-F", f"sub_issue_id={child_id}")
            print(f"#{child} under #{parent}", flush=True)
        except RuntimeError as e:
            print(f"relink #{child} under #{parent}: {e}", flush=True)


def main() -> None:
    if "--fixup" in sys.argv:
        fixup()
        return
    todo = sorted((i for r in SOURCES for i in issues(r)), key=lambda i: i["createdAt"])
    if not DUMP.exists():
        DUMP.write_text(json.dumps(todo, indent=1))
    print(f"{len(todo)} issues to transfer, {sum(1 for i in todo if i['parent'])} with a parent", flush=True)
    if "--apply" in sys.argv:
        apply(todo)


main()
