"""Download the supported e5 ONNX exports and their tokenizer vocabularies."""

import hashlib
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PRECISIONS = ("fp32", "fp16", "int8")
DEFAULT_PRECISION = "int8"
DEFAULT_MODEL = "multilingual-e5-small"

DOWNLOAD_CHUNK_BYTES = 1 << 20
DOWNLOAD_TIMEOUT_SECONDS = 60


class ModelDownloadError(RuntimeError):
    """A model file could not be downloaded, verified or written."""


@dataclass(frozen=True)
class ModelFile:
    remote: str
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelSpec:
    repo: str
    revision: str
    tokenizer: ModelFile
    exports: dict[str, tuple[ModelFile, ...]]

    def precisions(self) -> tuple[str, ...]:
        return tuple(precision for precision in PRECISIONS if precision in self.exports)


def _export(remote: str, name: str, size: int, sha256: str) -> ModelFile:
    return ModelFile(remote=remote, name=name, size=size, sha256=sha256)


_SENTENCEPIECE = ModelFile(
    remote="sentencepiece.bpe.model",
    name="sentencepiece.bpe.model",
    size=5_069_051,
    sha256="cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
)
_WORDPIECE = ModelFile(
    remote="vocab.txt",
    name="vocab.txt",
    size=231_508,
    sha256="07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
)

# Revisions are pinned so the checksums below stay valid, and so a later upstream
# replacement cannot silently change the vectors an index was built with.
MODEL_SPECS = {
    "multilingual-e5-small": ModelSpec(
        repo="Xenova/multilingual-e5-small",
        revision="761b726dd34fb83930e26aab4e9ac3899aa1fa78",
        tokenizer=_SENTENCEPIECE,
        exports={
            "fp32": (
                _export(
                    "onnx/model.onnx",
                    "model.onnx",
                    470_268_533,
                    "4aa845c27760e06e9a686b9d8b5d440eae4b6612cd09e5b522b716d3941f77ff",
                ),
            ),
            "fp16": (
                _export(
                    "onnx/model_fp16.onnx",
                    "model.onnx",
                    235_336_732,
                    "0e0fe349c99ea21c6f3aa273af21f7fb753c1e1174ef1647032029c2be3251c3",
                ),
            ),
            "int8": (
                _export(
                    "onnx/model_quantized.onnx",
                    "model.onnx",
                    118_308_185,
                    "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193",
                ),
            ),
        },
    ),
    "multilingual-e5-base": ModelSpec(
        repo="Xenova/multilingual-e5-base",
        revision="1ec9243030a27d1a115d5c340572074c125b58b2",
        tokenizer=_SENTENCEPIECE,
        exports={
            "fp32": (
                _export(
                    "onnx/model.onnx",
                    "model.onnx",
                    1_110_059_084,
                    "84a4d426f7e87a6bf5bf195f0bae2c4a7d15f675b23ca96f42fab8326d7a77aa",
                ),
            ),
            "fp16": (
                _export(
                    "onnx/model_fp16.onnx",
                    "model.onnx",
                    555_234_024,
                    "5d760477f691b665da2b94e1528eb6938b795f76064d9392e6af7118b8a3f54a",
                ),
            ),
            "int8": (
                _export(
                    "onnx/model_quantized.onnx",
                    "model.onnx",
                    278_647_662,
                    "df7a9a29309e3ad491e1783adf8baee710262cc06079c7cbab63c630277fac94",
                ),
            ),
        },
    ),
    "multilingual-e5-large": ModelSpec(
        repo="Xenova/multilingual-e5-large",
        revision="00fc3aeb3dbb95842de2ac1961d33c6319acf57b",
        tokenizer=_SENTENCEPIECE,
        exports={
            "fp32": (
                _export(
                    "onnx/model.onnx",
                    "model.onnx",
                    545_850,
                    "8e17fbd06c54c2a53b467f5b127230f798d4fb69c5344ef9a9d6fa463862c453",
                ),
                _export(
                    "onnx/model.onnx_data",
                    "model.onnx_data",
                    2_235_363_328,
                    "0cf1883fee81c63819a44e2ba0efa51d4043d9759685a4ebebbde97e0623d15c",
                ),
            ),
            "fp16": (
                _export(
                    "onnx/model_fp16.onnx",
                    "model.onnx",
                    1_118_202_847,
                    "6a7223bc225d1f26e09192a3b207be39019aea2a95e9014ce49e089cbbef01eb",
                ),
            ),
            "int8": (
                _export(
                    "onnx/model_quantized.onnx",
                    "model.onnx",
                    561_768_762,
                    "0a8d65db9a36f810ba5da15249f13145fcdc7890e6656f1fd38cd8b7c4db1fca",
                ),
            ),
        },
    ),
    "e5-small-v2": ModelSpec(
        repo="Xenova/e5-small-v2",
        revision="02af79985278377e65c724a76275707cb0333c70",
        tokenizer=_WORDPIECE,
        exports={
            "fp32": (
                _export(
                    "onnx/model.onnx",
                    "model.onnx",
                    133_093_467,
                    "b2a43b66f7f9b6f29643a21340ad7c03d2d91e6bd5d43429a77799a9ee880eb0",
                ),
            ),
            "fp16": (
                _export(
                    "onnx/model_fp16.onnx",
                    "model.onnx",
                    66_749_153,
                    "40f320a5c81339431d571af0e82eb2f68c6136d9aaa0ab40691efc56bf6cad07",
                ),
            ),
            "int8": (
                _export(
                    "onnx/model_quantized.onnx",
                    "model.onnx",
                    34_014_367,
                    "7d9092cb25f2bd1c023b7e8d2aa459044a02030ac880e5a59fdaf27af69f1ded",
                ),
            ),
        },
    ),
    "e5-base-v2": ModelSpec(
        repo="Xenova/e5-base-v2",
        revision="21f8d0e36fdfe76e6a023802dfb293fc6d750ad1",
        tokenizer=_WORDPIECE,
        exports={
            "fp32": (
                _export(
                    "onnx/model.onnx",
                    "model.onnx",
                    435_811_516,
                    "157f97ef1957d34f52efa26f8031371bf9043acc45460cec7ebe94631ac0e96b",
                ),
            ),
            "fp16": (
                _export(
                    "onnx/model_fp16.onnx",
                    "model.onnx",
                    218_108_177,
                    "823ade37a9d175334898ece493e2987984a56a5e3cf1b8c331bd8a6fabe9869c",
                ),
            ),
            "int8": (
                _export(
                    "onnx/model_quantized.onnx",
                    "model.onnx",
                    110_083_279,
                    "d0a7aadf8227f5212695488d99de54523678f5f9ecfb070f2b58cc76cd84d882",
                ),
            ),
        },
    ),
    "e5-large-v2": ModelSpec(
        repo="Xenova/e5-large-v2",
        revision="840fd2207f68e253697ed85392a482ff7657ad11",
        tokenizer=_WORDPIECE,
        exports={
            "fp32": (
                _export(
                    "onnx/model.onnx",
                    "model.onnx",
                    1_336_854_258,
                    "339329575d9c103ab4c08ae8d9d8334adaddda476d1d45f8c65abb7156445002",
                ),
            ),
            "fp16": (
                _export(
                    "onnx/model_fp16.onnx",
                    "model.onnx",
                    668_828_712,
                    "d269a78d3c58c9832c43b5be92279b5a5a4e6d60d08b1d36c1e4bc65888b3ceb",
                ),
            ),
            "int8": (
                _export(
                    "onnx/model_quantized.onnx",
                    "model.onnx",
                    336_983_104,
                    "80779f47fd485869cb5d12338e3956cab089075f136c39498f67d53bcc41b357",
                ),
            ),
        },
    ),
}

# The checksums documented for the default model. Other models are intentionally
# not listed: verify_model_files() warns for a model it does not recognize.
DEFAULT_MODEL_CHECKSUMS = {
    "model.onnx": frozenset(
        file.sha256
        for files in MODEL_SPECS[DEFAULT_MODEL].exports.values()
        for file in files
        if file.name == "model.onnx"
    ),
    "sentencepiece.bpe.model": MODEL_SPECS[DEFAULT_MODEL].tokenizer.sha256,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(path: Path, model_file: ModelFile) -> bool:
    try:
        if path.stat().st_size != model_file.size:
            return False
        return sha256_file(path) == model_file.sha256
    except OSError:
        return False


def _download_url(spec: ModelSpec, model_file: ModelFile) -> str:
    return (
        f"https://huggingface.co/{spec.repo}/resolve/"
        f"{spec.revision}/{model_file.remote}"
    )


def _progress(total: int, description: str):
    from tqdm import tqdm

    return tqdm(
        total=total,
        desc=description,
        unit="B",
        unit_scale=True,
        file=sys.stderr,
        disable=not sys.stderr.isatty(),
    )


def _download_file(
    spec: ModelSpec,
    model_file: ModelFile,
    destination: Path,
    force: bool,
) -> bool:
    """Download and verify one file; return True when the file was written."""
    if destination.exists() and not force:
        if _file_matches(destination, model_file):
            logger.info("zimantic: %s is already present", destination)
            return False
        raise ModelDownloadError(
            f"{destination} exists but does not match the expected {model_file.name} "
            "checksum; pass --force to replace it"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    url = _download_url(spec, model_file)
    request = urllib.request.Request(url, headers={"User-Agent": "zimantic"})
    logger.info("zimantic: downloading %s", url)
    try:
        with urllib.request.urlopen(
            request, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response, open(partial, "wb") as output:
            with _progress(model_file.size, model_file.name) as progress:
                while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
                    output.write(chunk)
                    progress.update(len(chunk))
    except (urllib.error.URLError, OSError) as error:
        partial.unlink(missing_ok=True)
        raise ModelDownloadError(f"could not download {url}: {error}") from error

    try:
        actual_size = partial.stat().st_size
        actual_sha256 = sha256_file(partial)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise ModelDownloadError(f"could not verify {destination}: {error}") from error
    if actual_size != model_file.size or actual_sha256 != model_file.sha256:
        partial.unlink(missing_ok=True)
        raise ModelDownloadError(
            f"downloaded {url} does not match the expected size or checksum "
            f"(got {actual_size} bytes, sha256 {actual_sha256})"
        )
    os.replace(partial, destination)
    return True


def model_root(model_dir: Path) -> Path:
    """Return the folder that holds the per-model, per-precision folders.

    The configured ``model_dir`` usually points at a precision folder (for
    example ``./model/int8``) or at a model and precision folder (for example
    ``./model/multilingual-e5-base/int8``). Running the command again must then
    write into the same folder rather than adding another level.
    """
    root = model_dir
    if root.name in PRECISIONS:
        root = root.parent
    if root.name in MODEL_SPECS:
        root = root.parent
    return root


def default_output_dir(model: str, model_dir: Path) -> Path:
    """Return the destination root for an export that was not given --dir.

    The default model writes ``<model_dir>/<precision>`` so the documented
    default layout stays short. Other models get their own folder under the
    same root, so downloading a second model cannot replace the first.
    """
    root = model_root(model_dir)
    return root if model == DEFAULT_MODEL else root / model


def download_model(
    model: str,
    precisions: list[str] | tuple[str, ...],
    output_dir: Path,
    force: bool = False,
) -> list[tuple[str, Path]]:
    """Download the selected exports into ``output_dir/<precision>``.

    Returns the precision and directory for every requested export. The tokenizer
    vocabulary is written beside each ``model.onnx`` so every folder is a complete
    ``model_dir``.
    """
    spec = MODEL_SPECS.get(model)
    if spec is None:
        supported = ", ".join(sorted(MODEL_SPECS))
        raise ModelDownloadError(
            f"unknown model {model!r} (use one of {supported})"
        )
    unknown = [precision for precision in precisions if precision not in PRECISIONS]
    if unknown:
        supported = ", ".join(PRECISIONS)
        raise ModelDownloadError(
            f"unknown precision {unknown[0]!r} (use {supported})"
        )
    unavailable = [precision for precision in precisions if precision not in spec.exports]
    if unavailable:
        raise ModelDownloadError(
            f"{model} has no {unavailable[0]} export "
            f"(available: {', '.join(spec.precisions())})"
        )
    if not precisions:
        raise ModelDownloadError("at least one precision is required")

    downloaded: list[tuple[str, Path]] = []
    for precision in precisions:
        directory = output_dir / precision
        files = [*spec.exports[precision], spec.tokenizer]
        # Write external data and the tokenizer first. The graph is the last
        # file replaced, so an interrupted --force refresh cannot leave a new
        # graph pointing at missing or old weights.
        files.sort(key=lambda model_file: model_file.name == "model.onnx")
        for model_file in files:
            _download_file(spec, model_file, directory / model_file.name, force)
        downloaded.append((precision, directory))
    return downloaded
