"""Omelette cooking example using threads and locks."""

import random
from threading import Condition, RLock, Thread
import time
from typing import Mapping, NamedTuple


class Ingredient:
    """A cooking ingredient with a name, state, quantity, and lock."""

    def __init__(self, name: str, quantity=1):
        """Initialize an ingredient."""
        self.name = name
        self.state_value = "raw"
        self.quantity = quantity
        self.lock = RLock()
        self.condition = Condition(self.lock)

    def __repr__(self):
        """Return a debug representation."""
        return f"Ingredient(name={self.name}, quantity={self.quantity})"

    def __str__(self):
        """Return a human-readable description."""
        if self.quantity == 1:
            return f"{self.state} {self.name}"

        return f"{self.quantity} {self.state} {self.name}s"

    @property
    def state(self):
        """Get the current state of the ingredient."""
        assert self.lock.locked
        return self.state_value

    @state.setter
    def state(self, value: str):
        assert self.lock.locked
        print("Changing", self.name, "state from", self.state_value, "to", value)
        self.state_value = value
        with self.condition:
            self.condition.notify_all()


class Utensil:
    """A kitchen utensil with a lock for thread safety."""

    def __init__(self, name: str):
        """Initialize a utensil."""
        self.name = name
        self.lock = RLock()

    def dice(self, ingredient: Ingredient):
        """Dice an ingredient using a knife."""
        assert self.name == "knife" and self.lock.locked
        print("Dicing", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "diced"

    def chop(self, ingredient: Ingredient):
        """Chop an ingredient using a knife."""
        assert self.name == "knife" and self.lock.locked
        print("Chopping", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "chopped"

    def beat(self, ingredient: Ingredient):
        """Beat an ingredient using a whisk."""
        assert self.name == "whisk" and self.lock.locked
        print("Beating", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "beaten"

    def grate(self, ingredient: Ingredient):
        """Grate an ingredient using a grater."""
        assert self.name == "grater" and self.lock.locked
        print("Grating", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "grated"


class Recipe(NamedTuple("Recipe", [("name", str), ("ingredients", Mapping[str, str])])):
    """A recipe with required ingredients and their expected states."""

    def check(self, ingredients: tuple[Ingredient, ...]) -> bool:
        """Verify that all ingredients are present and correctly prepared."""
        valid = set()
        for i in ingredients:
            if i.name not in self.ingredients:
                print(i.name, "is not used in", self.name)
                return False

            if i.state != self.ingredients[i.name]:
                print(i.name, "must be", self.ingredients[i.name])
                return False

            valid.add(i.name)

        if len(valid) < len(self.ingredients):
            print("missing ingredients:")
            for name, state in self.ingredients.items():
                if name not in valid:
                    print(state, name)

            return False

        return True


class Cookware:
    """A piece of cookware with a lock for thread safety."""

    def __init__(self, name: str):
        """Initialize cookware."""
        self.name = name
        self.lock = RLock()

    def cook(self, recipe: Recipe, ingredients: tuple[Ingredient, ...]):
        """Cook a recipe if all ingredients are ready."""
        assert self.lock.locked
        if not recipe.check(ingredients):
            return False

        print("All ingredients ready, cooking", recipe.name)
        time.sleep(random.random())
        for i in ingredients:
            i.state = "cooked"


def main():
    """Set up ingredients and run two cooks on separate threads."""
    onion = Ingredient("onion")
    pepper = Ingredient("pepper")
    eggs = Ingredient("egg", 3)
    cheese = Ingredient("cheese")
    knife = Utensil("knife")
    whisk = Utensil("whisk")
    grater = Utensil("grater")
    pan = Cookware("pan")
    omelette = Recipe("omelette", {
        "onion": "diced",
        "pepper": "chopped",
        "egg": "beaten",
        "cheese": "grated"
    })

    def cook1():
        print("Cook 1 starting")
        print("Cook 1: Dicing the onion")
        with onion.lock:
            with knife.lock:
                knife.dice(onion)

        print("Cook 1: Grating the cheese")
        with cheese.lock:
            with grater.lock:
                grater.grate(cheese)

        print("Cook 2 finished")

    def wait_until_ready(ingredient: Ingredient, state: str):
        print("Waiting until", ingredient.name, "is", state)
        with ingredient.lock:
            if ingredient.state == state:
                return
            else:
                print(ingredient.name, "is", ingredient.state, " waiting...")

        with ingredient.condition:
            while ingredient.state != state:
                print(ingredient.name, "is", ingredient.state, " waiting...")
                ingredient.condition.wait()

    def cook2():
        print("Cook 2 starting")

        print("Cook 2: Beating the eggs")
        with eggs.lock:
            with whisk.lock:
                whisk.beat(eggs)

        print("Cook 2: Chopping the pepper")
        with pepper.lock:
            with knife.lock:
                knife.chop(pepper)

        print("Cook 2: Waiting for other ingredients")
        wait_until_ready(onion, "diced")
        wait_until_ready(cheese, "grated")

        with onion.lock:
            with pepper.lock:
                with eggs.lock:
                    with cheese.lock:
                        with pan.lock:
                            pan.cook(omelette, (onion, pepper, eggs, cheese))

        print("Cook 2 finished")

    cook1_thread = Thread(target=cook1)
    cook2_thread = Thread(target=cook2)

    cook1_thread.start()
    cook2_thread.start()

    cook1_thread.join()
    cook2_thread.join()


if __name__ == "__main__":
    main()
