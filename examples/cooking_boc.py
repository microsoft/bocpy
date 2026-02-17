import random
import time
from typing import Mapping, NamedTuple, Tuple

from boc import Cown, start, wait, when


class Ingredient:
    def __init__(self, name: str, quantity=1):
        self.name = name
        self.state = "raw"
        self.quantity = quantity

    def __repr__(self):
        return f"Ingredient(name={self.name}, quantity={self.quantity})"

    def __str__(self):
        if self.quantity == 1:
            return f"{self.state} {self.name}"

        return f"{self.quantity} {self.state} {self.name}s"


class Utensil:
    def __init__(self, name: str):
        self.name = name

    def dice(self, ingredient: Ingredient):
        assert self.name == "knife"
        print("Dicing", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "diced"

    def chop(self, ingredient: Ingredient):
        assert self.name == "knife"
        print("Chopping", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "chopped"

    def beat(self, ingredient: Ingredient):
        assert self.name == "whisk"
        print("Beating", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "beaten"

    def grate(self, ingredient: Ingredient):
        assert self.name == "grater"
        print("Grating", str(ingredient))
        time.sleep(random.random())
        ingredient.state = "grated"


class Recipe(NamedTuple("Recipe", [("name", str), ("ingredients", Mapping[str, str])])):
    def check(self, ingredients: Tuple[Ingredient, ...]) -> bool:
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
    def __init__(self, name: str):
        self.name = name

    def cook(self, recipe: Recipe, ingredients: Tuple[Ingredient, ...]):
        if not recipe.check(ingredients):
            return False

        print("All ingredients ready, cooking", recipe.name)
        time.sleep(random.random())
        for i in ingredients:
            i.state = "cooked"


def main():
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
    def dice_onion(knife, onion):
        knife.value.dice(onion.value)

    @when(knife, pepper)
    def chop_pepper(knife, pepper):
        knife.value.chop(pepper.value)

    @when(whisk, eggs)
    def beat_eggs(whisk, eggs):
        whisk.value.beat(eggs.value)

    @when(grater, cheese)
    def grate_cheese(grater, cheese):
        grater.value.grate(cheese.value)

    @when(onion, pepper, eggs, cheese, pan)
    def cook_omelette(onion, pepper, eggs, cheese, pan):
        pan.value.cook(omelette, (onion.value, pepper.value, eggs.value, cheese.value))


if __name__ == "__main__":
    start(worker_count=2)
    main()
    wait()
