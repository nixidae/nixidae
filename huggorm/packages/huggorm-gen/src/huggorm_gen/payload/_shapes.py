"""`huggorm_dsl.shapes`, as the generator imports it.

pygen copies that file, not this one, into the generated package as
`_shapes.py`, so `_callspec` reads the same classes in both places.
"""

from huggorm_dsl.shapes import Builds as Builds
from huggorm_dsl.shapes import Entries as Entries
from huggorm_dsl.shapes import Items as Items
from huggorm_dsl.shapes import Leaf as Leaf
from huggorm_dsl.shapes import Null as Null
from huggorm_dsl.shapes import Subscription as Subscription
from huggorm_dsl.shapes import Tree as Tree
