from dataclasses import dataclass

from .utils import format_name, validate_email


@dataclass
class User:
    name: str
    email: str

    def display_name(self) -> str:
        return format_name(self.name)

    def has_valid_email(self) -> bool:
        return validate_email(self.email)
