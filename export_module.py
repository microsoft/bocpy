"""Tool for viewing and debugging BOC module export.

This tool is intended for advanced users. The result of passing it a
Python module file which contains behaviors will be an exported
version of the file in the same format into which the boc scheduler
will transpile it for inclusion on worker nodes. Seeing this file can
help debug why a particular file is not working as intended.
"""

import argparse

from boc.transpiler import export_module_from_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser("Export Test")
    parser.add_argument("path", type=str, help="Path to module file")
    parser.add_argument("--output", "-o", type=str, default="boc_export.py", help="Path to boc exported module")
    args = parser.parse_args()

    export = export_module_from_file(args.path)
    with open(args.output, "w") as file:
        file.write(export.code)

    print(export.classes)
    print(export.functions)
    print(export.behaviors)
