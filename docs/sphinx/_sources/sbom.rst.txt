.. _sbom:

Software Bill of Materials (SBOM)
=================================

Every official ``bocpy`` wheel ships with a Software Bill of Materials
(SBOM) embedded inside the distribution itself, following
`PEP 770 <https://peps.python.org/pep-0770/>`_.  The SBOM is a
machine-readable inventory of what the wheel contains and how it was
built, suitable for consumption by supply-chain tooling such as
`grype <https://github.com/anchore/grype>`_,
`Dependency-Track <https://dependencytrack.org>`_, or
`Trivy <https://github.com/aquasecurity/trivy>`_.

At a glance
-----------

.. list-table::
   :widths: 30 70
   :header-rows: 0

   * - Format
     - `CycloneDX 1.6 JSON <https://cyclonedx.org/specification/overview/>`_
   * - Location inside the wheel
     - ``<dist>-<version>.dist-info/sboms/bocpy.cdx.json``
   * - Filename convention
     - PEP 770: ``<filename>.cdx.json`` (no further suffix)
   * - Generator
     - `scripts/build_sbom.py <https://github.com/microsoft/bocpy/blob/main/scripts/build_sbom.py>`_
       (stdlib-only)
   * - Generator version
     - Recorded in ``metadata.tools.components[].version``
   * - Validator
     - `scripts/validate_sbom.py <https://github.com/microsoft/bocpy/blob/main/scripts/validate_sbom.py>`_
       (stdlib-only structural validator)

What the SBOM contains
----------------------

bocpy has **zero third-party runtime Python dependencies**: it ships
only stdlib usage and its own C extensions.  The SBOM therefore
describes a single root component — the wheel itself — and an empty
``components`` list:

.. code-block:: json

   {
     "bomFormat": "CycloneDX",
     "specVersion": "1.6",
     "serialNumber": "urn:uuid:…",
     "version": 1,
     "metadata": {
       "timestamp": "2026-…",
       "tools": {
         "components": [
           {
             "type": "application",
             "name": "build_sbom.py",
             "version": "0.1.0",
             "vendor": "Microsoft"
           }
         ]
       },
       "component": {
         "bom-ref": "pkg:pypi/bocpy@<version>",
         "type": "library",
         "name": "bocpy",
         "version": "<version>",
         "purl": "pkg:pypi/bocpy@<version>",
         "description": "…",
         "licenses": [{"license": {"id": "MIT"}}],
         "supplier": {"name": "Microsoft", "url": ["…homepage…"]},
         "externalReferences": [
           {"type": "website", "url": "…homepage…"},
           {"type": "vcs", "url": "…repo…"}
         ],
         "properties": [
           {"name": "cdx:python:git_commit",     "value": "<sha>"},
           {"name": "cdx:python:wheel_filename", "value": "<wheel filename>"}
         ]
       }
     },
     "components": [],
     "dependencies": [
       {"ref": "pkg:pypi/bocpy@<version>", "dependsOn": []}
     ]
   }

Two custom properties are attached to the root component:

``cdx:python:git_commit``
    The git commit SHA the wheel was built from.  Reproduces the
    exact source tree behind the wheel.

``cdx:python:wheel_filename``
    The basename of the wheel the SBOM was embedded in
    (e.g. ``bocpy-0.6.0-cp314-cp314-manylinux_2_28_x86_64.whl``).
    The CycloneDX ``purl`` field intentionally does not encode the
    wheel tag, so this property gives consumers the exact filename
    when they need it.

Native shared libraries that ``auditwheel``, ``delocate``, or
``delvewheel`` bundle into the wheel are **not** currently enumerated
in the SBOM ``components`` list.  Their presence is recoverable from
the wheel zip itself; future versions of the SBOM may add explicit
entries.

Extracting the SBOM from a wheel
--------------------------------

The SBOM is a plain file inside the wheel, so any zip extractor works:

.. code-block:: console

   $ unzip -p bocpy-0.6.0-cp314-cp314-manylinux_2_28_x86_64.whl \
       'bocpy-0.6.0.dist-info/sboms/bocpy.cdx.json' \
       | python -m json.tool | head -20

   {
       "bomFormat": "CycloneDX",
       "specVersion": "1.6",
       …
   }

For automated use, ``python -m zipfile`` works without any third-party
dependency:

.. code-block:: console

   $ python -m zipfile -e bocpy-0.6.0-cp314-cp314-manylinux_2_28_x86_64.whl /tmp/extracted/
   $ cat /tmp/extracted/bocpy-0.6.0.dist-info/sboms/bocpy.cdx.json

Verifying the SBOM yourself
---------------------------

The release workflow runs two checks on every batch of wheels before
they are published — you can reproduce both locally.

**1. Structural validation.**  ``scripts/validate_sbom.py`` is
stdlib-only and pins the invariants bocpy commits to (CycloneDX 1.6,
``urn:uuid:<v4>`` serial, ISO 8601 ``Z``-suffix timestamp, root
``purl == bom-ref`` and both starting with ``pkg:pypi/bocpy@``, etc.):

.. code-block:: console

   $ python scripts/validate_sbom.py path/to/bocpy.cdx.json
   OK   path/to/bocpy.cdx.json

The validator also accepts a directory of ``*.cdx.json`` files:

.. code-block:: console

   $ python scripts/validate_sbom.py sboms/
   OK   sboms/bocpy-0.6.0-cp310-cp310-manylinux_x86_64.cdx.json
   OK   sboms/bocpy-0.6.0-cp314-cp314-linux_x86_64.cdx.json

**2. Vulnerability scan.**  Any third-party SBOM-aware scanner will
parse the embedded file.  With grype:

.. code-block:: console

   $ grype sbom:./bocpy-0.6.0.dist-info/sboms/bocpy.cdx.json
   No vulnerabilities found

CI in ``.github/workflows/build_wheels.yml`` runs the same two checks
in the ``verify_sboms`` job after every successful wheel build.  The
job is configured with ``--fail-on high``: a HIGH or CRITICAL finding
fails the workflow.

Regenerating an SBOM by hand
----------------------------

The release workflow drives ``scripts/build_sbom.py`` via
``cibuildwheel``'s repair step, but the script is also runnable
standalone for testing or for downstream re-packaging:

.. code-block:: console

   $ python scripts/build_sbom.py generate \
       --wheel-filename bocpy-0.6.0-cp314-cp314-manylinux_2_28_x86_64.whl \
       --git-commit "$(git rev-parse HEAD)" \
       > bocpy.cdx.json

   $ python scripts/build_sbom.py inject \
       path/to/bocpy-0.6.0-cp314-cp314-manylinux_2_28_x86_64.whl

The ``inject`` subcommand rewrites the wheel's ``RECORD`` to add the
new SBOM entry atomically (writes a temporary wheel alongside, then
renames over the original only after the temp file is fully flushed
and closed).  An optional ``--copy-to DIR`` mode performs the
injection into a copy of the wheel in ``DIR``, leaving the original
untouched — this is how the Windows ``CIBW_REPAIR_WHEEL_COMMAND_WINDOWS``
moves repaired wheels to the cibuildwheel ``{dest_dir}``.

Wheel integrity validation
--------------------------

Because ``build_sbom.py inject`` rewrites every wheel's ``RECORD``
file after ``auditwheel`` / ``delocate`` / ``delvewheel`` have already
rewritten the ZIP, every release candidate goes through one more gate
before it can ship:

.. code-block:: console

   $ python scripts/validate_wheel.py path/to/wheelhouse/
   OK   wheelhouse/bocpy-0.7.0-cp314-cp314-manylinux_2_28_x86_64.whl
   OK   wheelhouse/bocpy-0.7.0-cp314-cp314-win_amd64.whl

``scripts/validate_wheel.py`` is a thin CLI driver over
``scripts/_vendored_warehouse_wheel.py`` — a stdlib-only,
verbatim-vendored copy of PyPI / Warehouse's own
``validate_record`` and ``validate_entrypoints``.  Running it locally
is the only reliable way to predict whether PyPI will accept a wheel:
``twine check`` only validates ``long_description``, ``wheel unpack``
only checks RECORD hashes and sizes, and ``check-wheel-contents`` only
checks layout — none of them runs PyPI's actual acceptance code.

The vendored file's docstring records the exact upstream commit it
was synced from and the refresh procedure.  The wheel-integrity job
in ``.github/workflows/build_wheels.yml`` runs it twice per release:
once inside ``CIBW_REPAIR_WHEEL_COMMAND`` (so a per-platform build
fails immediately on any defect) and once again in the ``merge`` job
(defense in depth — the last gate before the ``wheels`` artifact is
uploaded).

.. note::

   Per-component hash drift between 0.7.0 and 0.8.0+: ``_math.*.so``
   is now compiled with ``-O3`` (was ``-O2`` in 0.7.0) so its
   SHA-256 in any auditor's component diff will change even when
   nothing else moved. The flag is pinned in ``setup.py`` and scoped
   to ``_math`` only; ``_core.*.so`` is unaffected. See the comment
   in ``setup.py`` for the rationale (``-fvect-cost-model=very-cheap``
   at ``-O2`` declines to vectorise the M1 aggregate kernels).

See also
--------

* :ref:`api` — bocpy's public Python API.
* `SUPPLY_CHAIN.md
  <https://github.com/microsoft/bocpy/blob/main/SUPPLY_CHAIN.md>`_ —
  the full supply-chain hardening policy, covering hashed
  ``ci/constraints-*.txt`` files, SHA-pinned GitHub Actions, the
  ``pip-audit`` job, and the downstream consumer template.
* `PEP 770 <https://peps.python.org/pep-0770/>`_ — Embedding SBOMs in
  Python wheels.
* `CycloneDX 1.6 specification
  <https://cyclonedx.org/specification/overview/>`_.
