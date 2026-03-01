from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            name="boc._core",
            sources=["src/boc/_core.c"],
        ),
        Extension(
            name="boc._math",
            sources=["src/boc/_math.c"],
        ),

    ]
)
