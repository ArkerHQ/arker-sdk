"""Translate VM admission failures at the Modal exec boundary."""

from functools import wraps

from ._compat import import_optional
from .computer import ArkerError


def _missing_vm(error):
    return (
        isinstance(error, ArkerError)
        and error.status == 404
        and error.code == "not_found"
        and error.body is not None
        and error.body.code == "not_found"
        and getattr(error.body.details, "resource", None) == "vm"
    )


def exec_errors(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except ArkerError as error:
            native = import_optional("modal.exception")
            if self._vm is None or native is None or not _missing_vm(error):
                raise
            try:
                self._vm.refresh()
            except Exception as observed:
                mapped = native.NotFoundError if _missing_vm(observed) else None
            else:
                mapped = native.ConflictError
            if mapped is None:
                raise
            message = "is no longer available" if mapped is native.NotFoundError else "is shutting down"
            raise mapped(f"Sandbox {self.object_id} {message}") from error

    return invoke
