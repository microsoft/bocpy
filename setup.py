from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            name="bocpy._core",
            sources=["src/bocpy/_core.c"],
        ),
        Extension(
            name="bocpy._math",
            sources=["src/bocpy/_math.c"],
        ),

    ]
)
