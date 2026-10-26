"""Print the `// vyos-config-version` footer a VyOS image writes under its config.

usage: python3 -I vyos-footer.py CACHE_PY

CACHE_PY is the image's vyos/xml_ref/cache.py. It is read with
ast.literal_eval, never run: it comes from a downloaded image.
"""

import ast
import sys

tree = ast.parse(open(sys.argv[1]).read())
reference = next(
    node.value
    for node in tree.body
    if isinstance(node, ast.Assign)
    and isinstance(node.targets[0], ast.Name)
    and node.targets[0].id == "reference"
)
versions = ast.literal_eval(reference)["component_version"]
# vyos/component_version.py finds the footer by this line. Without it the
# config counts as versionless, every migration runs, and firewall 12-to-13
# deletes each `state` leaf it finds.
print("// Warning: Do not remove the following line.")
print(
    '// vyos-config-version: "'
    + ":".join(f"{name}@{version}" for name, version in sorted(versions.items()))
    + '"'
)
