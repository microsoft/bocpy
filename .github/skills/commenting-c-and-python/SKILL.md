---
name: commenting-c-and-python
description: "Follow bocpy commenting and documentation conventions. Use when: adding comments to C or Python files, writing docstrings, documenting structs or classes, adding Doxygen doc-comments, using Sphinx param style, suppressing linter warnings with noqa, or following flake8 style rules (Q000, D205, D209, N802)."
---

# Commenting C and Python Files

This skill describes the commenting conventions used across the `bocpy` project.
Follow these patterns when adding or editing comments so the codebase stays
consistent.

## C Files (`_core.c`, `_math.c`)

### Function-Level Documentation — Doxygen `///` Style

Every non-trivial function gets a Doxygen doc-comment block **immediately above**
its signature. Use triple-slash `///` lines with `@` tags in this order:

1. `@brief` — one-line summary (sentence case, **no** trailing period)
2. `@details` — optional longer explanation
3. `@note` — optional caveats or side-effects
4. `@param` — one per parameter (description starts uppercase)
5. `@return` — what the function returns

```c
/// @brief Creates a new BOCTag object from a Python Unicode string
/// @details The result object will not be dependent on the argument in any way
/// (i.e., it can be safely deallocated).
/// @param unicode A PyUnicode object
/// @param queue The queue to associate with this tag
/// @return a new BOCTag object
BOCTag *tag_from_PyUnicode(PyObject *unicode, BOCQueue *queue) {
```

For short helper functions, `@brief` alone is enough:

```c
/// @brief Convenience method to obtain the interpreter ID
/// @return the ID of the currently running interpreter
static inline PY_INT64_T get_interpid() {
```

### Struct Field Documentation

Document struct fields with `/// @brief` (and optionally `/// @details`) on
the line(s) **above** the field:

```c
typedef struct boc_message {
  /// @brief The tag associated with this message.
  /// @details This will be used by processes calling receive() and will create
  /// an affinity with a queue.
  struct boc_tag *tag;
  /// @brief whether the contents of this message were pickled
  bool pickled;
  /// @brief the threadsafe cross-interpreter data (the contents of the message)
  XIDATA_T *xidata;
} BOCMessage;
```

### Inline Comments — `//` Style

Use `//` for short explanatory comments inside function bodies. Place them on
the line **above** the code they describe, at the current indentation level:

```c
  // two possibilities:
  // 1. queue is empty
  // 2. queue is inconsistent

  // step 1: swap the new node in as the new head
  node->next = head;
```

End-of-line `//` comments are reserved for preprocessor version annotations:

```c
#if PY_VERSION_HEX >= 0x030E0000 // 3.14
```

### `/* */` Block Comments — Sentinel Only

Traditional block comments are used **exclusively** as sentinel markers in
`PyMethodDef` and slot arrays:

```c
    {NULL} /* Sentinel */
```

Do not use `/* */` for any other purpose.

### Method Table Doc Strings

Short one-phrase descriptions in `PyMethodDef` tables:

```c
static PyMethodDef CownCapsule_methods[] = {
    {"get", CownCapsule_get, METH_NOARGS, "internal"},
    {"set", CownCapsule_set, METH_VARARGS, "internal"},
    {"bid", BehaviorCapsule_bid, METH_NOARGS, "Gets the ID for the behavior"},
};
```

### Module Doc String

Set `.m_doc` in the `PyModuleDef` to a short description:

```c
    .m_doc = "Provides the underlying C implementation for the core BOC "
             "functionality",
```

### TODO Comments

Use `// TODO` on its own line, followed by the item(s):

```c
// TODO
// invert 2x2, 3x3, general algorithm for NxN
// det
// trace
```

---

## Python Files

### Module-Level Docstring

Every `.py` file starts with a **single-line** triple-quoted docstring.
Sentence case, ends with a period.

```python
"""Behavior-oriented Concurrency."""
```

```python
"""AST transformers that export when-decorated functions as behaviors."""
```

### Class Docstrings

Use a triple-quoted docstring immediately after the `class` statement. For
simple classes, a single-line docstring is preferred. For complex classes, use a
summary line, a blank line, then a longer description. Sentence case, ends with
a period.

```python
class Cown(Generic[T]):
    """Lightweight wrapper around the underlying cown capsule."""
```

```python
class BOCModuleTransformer(ast.NodeTransformer):
    """Prepares a main module for transpiling.

    This transformer collects the names of classes, functions, imports,
    and filters out everything else at the root level.
    """
```

### Function / Method Docstrings

Start with a **verb in imperative mood**. Single-line for simple functions,
multi-line for complex ones. Ends with a period.

**Single-line:**

```python
def acquire(self):
    """Acquires the cown (required for reading and writing)."""
```

**Multi-line (summary + body):**

```python
def release(self):
    """Release the cown to the next behavior.

    This is called when the associated behavior has completed, and thus can
    allow any waiting behavior to run.

    If there is no next behavior, then the cown's `last` pointer is set to null.
    """
```

### Parameter Documentation — Sphinx Style

Use Sphinx `:param:` / `:type:` / `:return:` / `:rtype:` fields for parameter
documentation. This is the single accepted style across the project.

```python
def export_module(tree: ast.Module, path: str = None) -> ExportResult:
    """Extract an AST as a BOC-enlightened module with generated behaviors.

    :param tree: The source tree
    :type tree: ast.Module
    :return: An export result with code and metadata
    :rtype: ExportResult
    """
```

```python
def __init__(self, num_workers: Optional[int], export_dir: Optional[str]):
    """Creates a new Behaviors scheduler.

    :param num_workers: The number of worker interpreters to start.  If
        None, defaults to the number of available cores minus one.
    :type num_workers: Optional[int]
    :param export_dir: The directory to which the target module will be
        exported for worker import.  If None, a temporary directory will
        be created and removed on shutdown.
    :type export_dir: Optional[str]
    """
```

### `.pyi` Stub File Docstrings

The stub file `__init__.pyi` uses **Sphinx-style** docstrings on every public
function, class, and constant. Constants use an attribute docstring on the line
after the declaration:

```python
TIMEOUT: str
"""Sentinel value returned by :func:`receive` when a timeout occurs."""
```

```python
def send(tag: str, contents: Any):
    """Sends a message.

    :param tag: The tag is an arbitrary label that can be used to receive this message.
    :type tag: str
    :param contents: The contents of the message.
    :type contents: Any
    """
```

### Inline `#` Comments

Use `#` comments for short notes inside function bodies. Place them on the line
above the code, at the current indentation:

```python
        orphan_cowns = _core.cowns()
        if len(orphan_cowns) != 0:
            logger.debug("acquiring orphan cowns")
            # at this stage all behaviors have exited, but it may be the case
            # that some cowns are released but associated with this interpreter.
            # by acquiring them, we ensure that the XIData objects have been
            # freed _before_ this interpreter is destroyed.
```

Same-line `#` comments are acceptable for very short annotations:

```python
        except RuntimeError:
            pass  # already destroyed
```

### `# noqa:` Suppression Comments

Suppress linter warnings with `# noqa: CODE` at the end of the line.
Place the suppression on the **line that contains the violation**, not a
surrounding line:

```python
    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
```

```python
# CORRECT — noqa on the line that references the loop variable
@when(acc)
def _(a):
    a.value.add(val_to_add)  # noqa: B023

# WRONG — noqa on the def line does not suppress the reference on the next line
@when(acc)
def _(a):  # noqa: B023
    a.value.add(val_to_add)   # ← still triggers B023
```

### `# BEGIN` / `# END` Markers

Code-generation insertion points in `worker.py`:

```python
# BEGIN boc_export
# END boc_export
```

---

## Linting Rules (flake8)

The project enforces style with `flake8` (config in `.flake8`):

| Rule | Setting |
|------|---------|
| `inline-quotes` | `double` — use `"` not `'` (Q000) |
| `max-line-length` | 120 |
| `docstring-convention` | `google` (with Sphinx `:param:` fields — `napoleon` handles both) |
| `extend-ignore` | E203, N812, N817 |
| `per-file-ignores` | `test/*`: D103 (missing public-function docstring), D403 |

### Multi-line Class Docstrings (D205 / D209)

A multi-line docstring must have a **summary line**, a **blank line**, then the
body. The closing `"""` must be on its own line:

```python
# CORRECT
class Foo:
    """Short summary line.

    Longer description goes here after the blank line.
    """

# WRONG — triggers D205 and D209
class Foo:
    """This wraps onto a second line
    without a blank separator."""
```

### Naming in Test Files (N802)

Test function names must be **lowercase**. Even when testing a property like
`.T`, spell the test name with a lowercase letter:

```python
# CORRECT
def test_t_equals_transpose(self, mat): ...

# WRONG — triggers N802
def test_T_equals_transpose(self, mat): ...
```

---

## Quick Reference

| Element | C convention | Python convention |
|---------|-------------|-------------------|
| Function docs | `/// @brief` ... `/// @return` | `"""Imperative summary."""` |
| Struct/class docs | `/// @brief` above each field | `"""Single-line or multi-line."""` after `class` |
| Parameter docs | `/// @param name Description` | `:param name:` / `:type name:` (Sphinx) |
| Inline comments | `//` on line above code | `#` on line above code |
| End-of-line | `//` for preprocessor version notes | `#` for very short annotations or `# noqa:` |
| Block comments | `/* Sentinel */` only | — |
| Sentence case | `@brief` starts with uppercase | Docstrings start with uppercase |
| Trailing period | No period on `@brief` | Docstrings end with a period |
| TODOs | `// TODO` on its own line | `# TODO` on its own line |
| Docstring style | — | Sphinx `:param:` / `:type:` / `:return:` / `:rtype:` |

## Common Pitfalls

| Pitfall | Fix |
|---------|-----|
| Using `/* */` for documentation blocks in C | Use `///` Doxygen-style instead. `/* */` is only for `/* Sentinel */`. |
| Using Google-style `Args:` blocks | Use Sphinx `:param:` / `:type:` style instead. The project uses Sphinx `autodoc` for documentation. |
| Omitting the `@brief` tag in C doc-comments | Always start with `/// @brief`. It is the minimum for every documented function. |
| Forgetting the trailing period in Python docstrings | All Python docstrings end with a period. |
| Adding a trailing period to C `@brief` lines | C `@brief` descriptions do **not** end with a period. |
| Duplicating comments | Do not place a `//` block that repeats the `///` doc-comment below it. Write it once. |
| Placing `# noqa:` on the wrong line | Put it on the line with the actual violation, not a surrounding `def` or decorator line. |
| Multi-line class docstring without a blank line after the summary | Add a blank line between the summary sentence and the body (D205). Close `"""` on its own line (D209). |
| Using single quotes in Python | The project enforces double quotes (`inline-quotes = double`). Use `"nan"` not `'nan'`. |
| Assigning `Cown(m)` to an unused variable | If the return value is not needed (e.g., just releasing the matrix), call `Cown(m)` without assignment to avoid F841. |
