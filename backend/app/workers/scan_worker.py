"""Scanner worker placeholder.

Important lifecycle rule for future implementation:
- never pass a request-scoped SQLAlchemy session into background scan tasks
- scanner jobs must create and manage their own DB session lifecycle
- retry / backoff / upstream rate limiting will be implemented here in package 02
"""


def not_implemented_yet() -> dict:
    return {
        'status': 'todo',
        'message': 'scanner worker will be implemented in package 02',
    }
