from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            name="_boc",
            sources=["src/boc/boc.c"],
        ),
    ]
)
