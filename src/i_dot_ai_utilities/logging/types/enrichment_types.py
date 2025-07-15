from enum import Enum


class ContextEnrichmentType(Enum):
    FASTAPI = 1
    LAMBDA = 2


class ExecutionEnvironmentType(Enum):
    LOCAL = 1
    FARGATE = 2
    LAMBDA = 3
