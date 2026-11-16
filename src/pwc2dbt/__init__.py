"""Convert Informatica PowerCenter mapping exports into dbt models.

The shape is a compiler's, because that is what this is: one input artefact in,
generated source out. Each stage only knows the ones before it.

    parse      parse.py, validate.py      XML  -> objects
    represent  model.py                   the objects themselves
    analyse    graph.py, naming.py        which models, fed by which steps
    translate  expressions.py             Informatica expressions -> SQL
    generate   sql.py                     steps -> CTEs -> one model
    emit       emit.py                    the dbt project on disk

`reference/` is separate: a second implementation used to cross-check the
first. Nothing in the pipeline imports it.
"""

__version__ = "0.1.0"
