"""User model for the flask_starter fixture."""


# PLANTED ISSUE: mutable default argument (roles=[]).
class User:
    def __init__(self, username: str, email: str, roles=[]):
        self.username = username
        self.email = email
        self.roles = roles

    def add_role(self, role: str) -> None:
        self.roles.append(role)

    def has_role(self, role: str) -> bool:
        return role in self.roles
