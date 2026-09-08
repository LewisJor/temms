"""TEMMS exceptions."""


class TEMMSError(RuntimeError):
    """Base exception for the TEMMS kernel."""


class RuntimeContractError(TEMMSError):
    """The injected runtime violated the TEMMS runtime contract."""


class ModelUnavailableError(TEMMSError):
    """A runtime cannot access the requested model."""


class NoActiveModelError(TEMMSError):
    """Inference was requested before a model became active."""
