def format_name(name: str) -> str:
    return name.strip().title()


def validate_email(email: str) -> bool:
    return "@" in email and "." in email.split("@", 1)[-1]
