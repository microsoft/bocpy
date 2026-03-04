"""Various concurrency sketches inspired by Monty Python bits."""

import logging
import os
import random
import time

from bocpy import Cown, wait, when


def all_known_cheeses() -> list[str]:
    """Load the cheese inventory from disk."""
    path = os.path.join(os.path.dirname(__file__), "assets", "cheese.txt")
    with open(path) as file:
        return [line.strip() for line in file]


def is_available(logger, name: str) -> bool:
    """Check whether a cheese is in stock (mocked randomness)."""
    logger.info(f"{name}?")
    time.sleep(random.random() * 0.05)
    response = random.choice(["Not today, sir, no", "No", "Sorry"])
    logger.info(response)
    return False


def menu() -> list[str]:
    """Load the menu and shuffle it."""
    path = os.path.join(os.path.dirname(__file__), "assets", "menu.txt")
    with open(path) as file:
        menu = [line.strip() for line in file]
        random.shuffle(menu)
        return menu


def vikings(logger):
    """Log a random SPAM chant with some jitter."""
    spam = " ".join(["SPAM"] * random.randint(1, 5))
    lovely = "! LOVELY SPAM, WONDERFUL SPAM!" if random.random() < 0.2 else "!"
    logger.info(spam + lovely)
    time.sleep(random.random() * 0.2)


def cleanup_shop(logger):
    """Fallback handler when no cheese can be found."""
    logger.warning("<GUNSHOT>")
    logger.info("What a senseless waste of human life!")


def eat(food: str):
    """Consume and print the selected item."""
    print(f"Eating {food}")


def return_to_library():
    """Fallback action when no acceptable food is found."""
    print("Returning to the public library to resume skimming Rogue Herries")


def buy_cheese():
    """Attempt to buy any available cheese, otherwise clean up."""
    cheese = Cown(None)

    @when(cheese)
    def _(cheese):
        logger = logging.getLogger("cheese_shop")
        for name in all_known_cheeses():
            if is_available(logger, name):
                cheese.value = name
                return

        cleanup_shop(logger)

    return cheese


def order_meal(exclude: str):
    """Order a menu item that avoids the excluded ingredient."""
    order = Cown(None)

    @when(order)
    def _(order):
        logger = logging.getLogger("greasy_spoon")
        logger.info("We have...")
        for dish in menu():
            logger.info(dish)
            if exclude.lower() not in dish.lower():
                logger.info(f"That doesn't have much {exclude} in it")
                order.value = dish
                return

            vikings(logger)
            if random.random() < 0.3:
                logger.info("<bloody vikings>")

    return order


def main():
    """Run the cheese shop and greasy spoon sketches concurrently."""
    logging.basicConfig(level=logging.INFO)

    cheese = buy_cheese()
    meal = order_meal(exclude="spam")

    @when(cheese, meal)
    def _(cheese, meal):
        if meal.value is not None:
            eat(meal.value)
        elif cheese.value is not None:
            eat(cheese.value)
        else:
            print("<stomach rumbles>")

    @when(cheese, meal)
    def _(cheese, meal):
        if meal.value is not None:
            print("I really wanted cheese...")
        elif cheese.value is not None:
            print("Cheesy comestibles!")

        return_to_library()

    wait()


if __name__ == "__main__":
    main()
