"""Identity values that are scoped to one game session."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DMIdentity:
    """Reference to the current game controller, not a system administrator."""

    participant_id: str
    qq_id: str

    def __post_init__(self) -> None:
        if not self.participant_id:
            raise ValueError("participant_id must not be empty")
        if not self.qq_id:
            raise ValueError("qq_id must not be empty")
