# Contributing

This project welcomes contributions and suggestions. Most contributions require you to
agree to a Contributor License Agreement (CLA) declaring that you have the right to,
and actually do, grant us the rights to use your contribution. For details, visit
https://cla.microsoft.com.

When you submit a pull request, a CLA-bot will automatically determine whether you need
to provide a CLA and decorate the PR appropriately (e.g., label, comment). Simply follow the
instructions provided by the bot. You will only need to do this once across all repositories using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/)
or contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Matrix micro-benchmark

`scripts/bench_matrix.py` is a standalone micro-benchmark covering the full
`Matrix` C-extension surface — properties, unary and binary ops, matmul,
aggregations, reshapes, factories, and the vector helpers (`length`,
`magnitude_squared`, `vecdot`, `cross`, `normalize`, `perpendicular`,
`angle`). It exists as a point-in-time reference for tracking regressions in
`src/bocpy/_math.c`; it is **not** wired into CI (per-iteration variance on
shared runners is too high to gate on).

Run it after a clean build:

```bash
python scripts/bench_matrix.py > bench-results.txt
```

For archival or tooling, also emit a structured JSON file alongside the
text output:

```bash
python scripts/bench_matrix.py --json bench-results.json > bench-results.txt
```

When a change in `_math.c` shifts a number meaningfully, paste the relevant
lines (or attach the JSON) in the PR description rather than committing the
result files.
