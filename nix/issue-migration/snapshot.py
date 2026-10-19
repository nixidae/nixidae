#!/usr/bin/env python3
"""Snapshot every nixidae/nixidae issue with body and comments to snapshot.json."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

Q = """
query($after:String){
  repository(owner:"nixidae",name:"nixidae"){
    issues(first:25,after:$after,orderBy:{field:CREATED_AT,direction:ASC}){
      pageInfo{hasNextPage endCursor}
      nodes{id number title url state stateReason createdAt body issueType{name}
            labels(first:20){nodes{name}} parent{number}
            comments(first:100){totalCount nodes{id url createdAt author{login} body}}}
    }
  }
}"""

out, after = [], None
while True:
    args = ["gh", "api", "graphql", "-f", f"query={Q}"] + (["-f", f"after={after}"] if after else [])
    page = json.loads(subprocess.run(args, capture_output=True, text=True, check=True).stdout)
    page = page["data"]["repository"]["issues"]
    out += page["nodes"]
    if not page["pageInfo"]["hasNextPage"]:
        break
    after = page["pageInfo"]["endCursor"]
truncated = [i["number"] for i in out if i["comments"]["totalCount"] > len(i["comments"]["nodes"])]
Path(__file__).with_name("snapshot.json").write_text(json.dumps(out, indent=1))
print(f"{len(out)} issues, comments truncated on {truncated}")
