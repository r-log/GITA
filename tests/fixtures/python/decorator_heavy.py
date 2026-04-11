"""Decorators and nested functions."""
import functools


def my_decorator(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


@my_decorator
def decorated_function():
    pass


class Service:
    @staticmethod
    def static_method():
        pass

    @classmethod
    def class_method(cls):
        pass

    @property
    def name(self) -> str:
        return "service"
