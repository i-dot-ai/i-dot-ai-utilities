ContextFieldPrimitives = str | int | bool | float
ContextFieldValue = (
    ContextFieldPrimitives
    | list[ContextFieldPrimitives]
    | dict[str, ContextFieldPrimitives]
)
