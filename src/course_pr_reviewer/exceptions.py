"""Domain exceptions raised by the reviewer."""


class ReviewerError(Exception):
    """Base exception for expected reviewer failures."""


class ConfigurationError(ReviewerError):
    """A course or roster configuration is invalid."""


class ReviewSystemError(ReviewerError):
    """The review could not be completed reliably."""


class ProviderUnavailableError(ReviewSystemError):
    """A configured provider is temporarily unavailable after retries."""


class ProviderConfigurationError(ReviewSystemError):
    """A provider cannot run because credentials or configuration are invalid."""


class ContentLimitExceeded(ReviewerError):
    """A submission cannot be reviewed completely within configured limits."""


class InvalidStudentImage(ReviewerError):
    """A submitted image is malformed or violates image safety limits."""
