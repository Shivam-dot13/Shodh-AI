"""Supported languages and the runner image that executes each one.

The image tag is written into every attempt runtime_config at submit time, so a verdict always
names the exact runner that produced it. Adding a language is: add an image under
runner-images/, add it here, add the env override in docker-compose.yml.
"""
from __future__ import annotations

import os

from .domain.enums import Language

DEFAULT_IMAGES: dict[Language, str] = {
    Language.PYTHON3: "shodh-runner-python:1",
}

_ENV_KEYS = {
    Language.PYTHON3: "RUNNER_IMAGE_PYTHON3",
}


def runner_image_for(language: Language) -> str:
    return os.environ.get(_ENV_KEYS[language], DEFAULT_IMAGES[language])


def supported_languages() -> dict[str, str]:
    return {lang.value: runner_image_for(lang) for lang in Language}
