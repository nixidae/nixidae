"""`huggorm_dsl.shapes`, as the generator imports it.

pygen copies that file, not this one, into the generated package as
`_shapes.py`, so `_callspec` reads the same classes in both places.
"""

from huggorm_dsl.shapes import (
    Builds as Builds,
    Entries as Entries,
    Items as Items,
    Leaf as Leaf,
    Null as Null,
    Scalar as Scalar,
    Subscription as Subscription,
    Tree as Tree,
)
