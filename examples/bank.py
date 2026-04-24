"""Example showing coordination over two bank accounts to facilitate a transfer."""

import argparse
import logging

from bocpy import Cown, wait, when


class Account:
    """Simple bank account with a name, balance, and frozen flag."""

    def __init__(self, name: str, balance: float, frozen=False):
        """Initialize an account with a starting balance."""
        self.name = name
        self.balance = balance
        self.frozen = frozen

    def __repr__(self) -> str:
        """Return a readable representation for debugging."""
        return f"Account(name='{self.name}', balance={self.balance}, frozen={self.frozen})"


def atomic_transfer(src: Cown[Account], dst: Cown[Account], amount: float):
    """Move funds from ``src`` to ``dst`` if both are unfrozen and funded."""
    @when(src, dst)
    def do_transfer(src: Cown[Account], dst: Cown[Account]):
        src_account = src.value
        dst_account = dst.value
        print("attempting to transfer", amount, "from", src_account.name, "to", dst_account.name)
        if src_account.balance > amount and not src_account.frozen and not dst_account.frozen:
            src_account.balance -= amount
            dst_account.balance += amount
            print("success")
        else:
            print("failure")

        @when(src)
        def _(a: Cown[Account]):
            print("src (after transfer):", a.value)

        @when(dst)
        def _(b: Cown[Account]):
            print("dst (after transfer):", b.value)


def check_balance(message: str, account: Cown[Account]):
    """Log the current balance of the provided account."""
    @when(account)
    def do_check(account: Cown[Account]):
        print(message, account.value)


def main(amount: int = None):
    """Parse arguments, set up accounts, transfer, and display balances."""
    parser = argparse.ArgumentParser("Bank Transfer")
    parser.add_argument("--amount", "-a", type=int, default=50)
    parser.add_argument("--loglevel", "-l", type=str, default=logging.WARNING)
    args = parser.parse_args()

    logging.basicConfig(level=args.loglevel)

    if amount is None:
        amount = args.amount

    alice = Cown(Account("Alice", 100))
    bob = Cown(Account("Bob", 0))

    check_balance("src (before transfer):", alice)
    check_balance("dst (before transfer):", bob)
    atomic_transfer(alice, bob, amount)
    wait()


if __name__ == "__main__":
    main()
