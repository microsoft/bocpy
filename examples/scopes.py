"""Return library-local results while root-owned work runs independently."""

from bocpy import Cown, root_scope, Scope, start, wait, when


def sum_squares(values: list[int], audit: Cown[list[str]]) -> int:
    """Quiesce two local batches, then return their final output."""
    with Scope(0) as local:
        midpoint = len(values) // 2
        for batch in (values[:midpoint], values[midpoint:]):
            for value in batch:
                @when(local.output_cown)
                def add_square(total: Cown[int], value=value):
                    total.value += value * value

            local.quiesce()

        count = len(values)

        @when(audit, scope=root_scope)
        def record(entries: Cown[list[str]], count=count):
            entries.value.append(f"processed {count} values")

    return local.output_value


def main():
    """Run a synchronous library call and finish background work globally."""
    start(2)
    audit = Cown([])
    try:
        answer = sum_squares([1, 2, 3, 4], audit)
        print(f"Local result: {answer}")
        assert answer == 30
    finally:
        wait()

    print(f"Root audit: {audit.unwrap()}")


if __name__ == "__main__":
    main()
