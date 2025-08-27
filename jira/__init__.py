class JIRA:  # minimal stub for tests
    def __init__(self, *args, **kwargs) -> None:
        pass


class Issue:  # minimal stub for tests
    pass


class JIRAError(Exception):
    pass

# Provide jira.resources.Issue import path compatibility
class resources:  # type: ignore
    Issue = Issue


