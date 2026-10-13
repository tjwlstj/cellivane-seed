"""Immutable artifact profiles, not general GGUF or backend qualification.

Model choice is explicit. No memory or quality policy silently substitutes a
smaller target. Only the named Qwen3 Q8_0 artifacts and bounded draft pairs
are accepted by the current owned CPU launcher.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

_PINS = MappingProxyType({
    "0.6b": ("Qwen3-0.6B-Q8_0.gguf", 639446688,
        "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"),
    "1.7b": ("Qwen3-1.7B-Q8_0.gguf", 1834426016,
        "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a"),
    "4b": ("Qwen3-4B-Q8_0.gguf", 4280404704,
        "8c2f07f26af9747e41988551106f149b03eb9b5cb6df636027b6bf6278473300"),
})
PROFILE_NAMES = tuple(_PINS)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelProfile:
    name: str
    filename: str
    bytes: int
    sha256: str

    def __post_init__(self):
        self.validate()

    def validate(self):
        if (type(self) is not ModelProfile or type(self.name) is not str or self.name not in _PINS
                or type(self.filename) is not str or type(self.bytes) is not int or type(self.sha256) is not str
                or (self.filename, self.bytes, self.sha256) != _PINS[self.name]):
            raise ValueError("model-profile-pin")

    def record(self):
        self.validate()
        return {"schema_version": 1, "name": self.name, "filename": self.filename,
                "bytes": self.bytes, "sha256": self.sha256,
                "scope": "pinned-model-artifact-not-backend-qualification"}


def get_profile(name):
    if type(name) is not str or name not in _PINS:
        raise ValueError("model-profile-name")
    filename, size, digest = _PINS[name]
    return ModelProfile(name=name, filename=filename, bytes=size, sha256=digest)


def identify_profile(size, digest):
    if type(size) is not int or type(digest) is not str:
        raise ValueError("model-profile-pin")
    for name, (_filename, known_size, known_digest) in _PINS.items():
        if (size, digest) == (known_size, known_digest):
            return get_profile(name)
    raise ValueError("model-profile-pin")


def validate_pair(target, draft=None):
    if type(target) is not ModelProfile or (draft is not None and type(draft) is not ModelProfile):
        raise ValueError("model-profile-pair-type")
    target.validate()
    if draft is not None:
        draft.validate()
        if target.name not in ("1.7b", "4b") or draft.name != "0.6b" or draft.bytes >= target.bytes:
            raise ValueError("model-profile-draft-pair-unsupported")
