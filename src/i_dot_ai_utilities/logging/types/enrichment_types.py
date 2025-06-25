from enum import Enum


class ContextEnrichmentType(Enum):
    FASTAPI = 1


class ExecutionEnvironmentType(Enum):
    LOCAL = 1
    FARGATE = 2
