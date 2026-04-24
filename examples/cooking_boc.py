"""Omelette cooking example using behavior-oriented concurrency."""

import random
import time
from typing import Mapping, NamedTuple

from bocpy import Cown, start, wait, when


class Ingredient:
    """A cooking ingredient with a name, state, and quantity."""

    def __init__(self, name: str, quantity=1):
        """Initialize an ingredient."""
        self.name = name
        self.state = "raw"
        self.quantity = quantity

    def __repr__(self):
        """Return a debug representation."""
        return f"Ingredient(name={self.name}, quantity={self.quantity})"

    def __str__(self):
        """Return a human-readable description."""
        if self.quantity == 1:
            return f"{self.state} {self.name}"

        return f"{self.quantity} {self.state} {self.name}s"


class Utensil:
    """A kitchen utensil that can perform actions on ingredients."""

    def __init__(self, name: str):
        """Initialize a utensil."""
        self.name = name

    def dice(self, ingredient: Ingredient):
        """Dice an ingredient using a knife."""
        assert self.name == "knife"
        print("Dicing", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "diced"

    def chop(self, ingredient: Ingredient):
        """Chop an ingredient using a knife."""
        assert self.name == "knife"
        print("Chopping", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "chopped"

    def beat(self, ingredient: Ingredient):
        """Beat an ingredient using a whisk."""
        assert self.name == "whisk"
        print("Beating", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "beaten"

    def grate(self, ingredient: Ingredient):
        """Grate an ingredient using a grater."""
        assert self.name == "grater"
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
    """A piece of cookware that can cook a recipe."""

    def __init__(self, name: str):
        """Initialize cookware."""
        self.name = name

    def cook(self, recipe: Recipe, ingredients: tuple[Ingredient, ...]):
        """Cook a recipe if all ingredients are ready."""
        if not recipe.check(ingredients):
            return False

        print("All ingredients ready, cooking", recipe.name)
        time.sleep(random.random())
        for i in ingredients:
            i.state = "cooked"


def main():
    """Set up ingredients and schedule cooking behaviors."""
    start(worker_count=2)

    onion = Cown(Ingredient("onion"))
    pepper = Cown(Ingredient("pepper"))
    eggs = Cown(Ingredient("egg", 3))
    cheese = Cown(Ingredient("cheese"))
    knife = Cown(Utensil("knife"))
    whisk = Cown(Utensil("whisk"))
    grater = Cown(Utensil("grater"))
    pan = Cown(Cookware("pan"))
    omelette = Recipe("omelette", {
        "onion": "diced",
        "pepper": "chopped",
        "egg": "beaten",
        "cheese": "grated"
    })

    @when(knife, onion)
    def dice_onion(knife: Cown[Utensil], onion: Cown[Ingredient]):
        knife.value.dice(onion.value)

    @when(knife, pepper)
    def chop_pepper(knife: Cown[Utensil], pepper: Cown[Ingredient]):
        knife.value.chop(pepper.value)

    @when(whisk, eggs)
    def beat_eggs(whisk: Cown[Utensil], eggs: Cown[Ingredient]):
        whisk.value.beat(eggs.value)

    @when(grater, cheese)
    def grate_cheese(grater: Cown[Utensil], cheese: Cown[Ingredient]):
        grater.value.grate(cheese.value)

    @when(onion, pepper, eggs, cheese, pan)
    def cook_omelette(onion: Cown[Ingredient], pepper: Cown[Ingredient],
                      eggs: Cown[Ingredient], cheese: Cown[Ingredient],
                      pan: Cown[Cookware]):
        pan.value.cook(omelette, (onion.value, pepper.value, eggs.value, cheese.value))

    wait()


if __name__ == "__main__":
    main()
