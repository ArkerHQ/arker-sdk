"""An omitted request field, distinct from an explicit JSON null."""

from enum import Enum


class Unset(Enum):
    TOKEN = "unset"


UNSET = Unset.TOKEN
