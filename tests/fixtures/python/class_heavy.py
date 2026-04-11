"""Classes with methods, including inheritance and async methods."""
from abc import ABC, abstractmethod


class Animal(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def sound(self) -> str:
        pass


class Dog(Animal):
    def sound(self) -> str:
        return "Woof"

    def fetch(self) -> None:
        print(f"{self.name} fetches")


class Cat(Animal):
    def sound(self) -> str:
        return "Meow"

    async def nap(self, duration: int) -> None:
        pass
