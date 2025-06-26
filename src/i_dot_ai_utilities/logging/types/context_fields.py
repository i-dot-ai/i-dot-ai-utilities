from collections.abc import Sequence

ContextFieldPrimitives = str | int | bool | float
ContextFieldValue = (
    ContextFieldPrimitives
    | Sequence[ContextFieldPrimitives]
    | dict[str, ContextFieldPrimitives]
)
