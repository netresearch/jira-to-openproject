"""Defines exceptions for the migration process."""


class MigrationError(Exception):
    """Base exception for migration errors.

    Should be used when a migration component encounters an error
    that prevents it from continuing execution.
    """

    def __init__(self, message: str, *args: object, **kwargs: object) -> None:
        """Initialize the exception with a descriptive message.

        Args:
            message: Detailed error message
            *args: Additional positional arguments for Exception
            **kwargs: Additional keyword arguments for Exception

        """
        super().__init__(message, *args, **kwargs)
        self.message = message
