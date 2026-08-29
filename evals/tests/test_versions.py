"""The corpus digest binds reviewed waivers to the content they reviewed.

``corpus_digest`` feeds ``manifest_hash``, which is what ``exceptions.json``
waivers and eval run ids are pinned to. A digest that ignored chunk text or
the active flag let a reviewed waiver silently apply to edited or
deactivated chunks no reviewer had seen (R-15), so the specification here
is: content edits move the digest (and the run id derived from it), and
nothing else does — not corpus order, not the interpreter's hash seed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from dataclasses import replace

from evals.compare import run_id
from evals.corpus import FixtureCorpus, load_corpus
from evals.retriever import baseline_config
from evals.versions import component_manifest, corpus_digest

_DIGEST_SNIPPET = (
    "from evals.corpus import load_corpus; "
    "from evals.versions import corpus_digest; "
    "print(corpus_digest(load_corpus()))"
)


class TestCorpusDigestTamper(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = load_corpus()

    def _edited_first_chunk(
        self, *, text: str | None = None, active: bool | None = None
    ) -> FixtureCorpus:
        target = self.corpus.chunks[0]
        edited = replace(
            target,
            text=target.text if text is None else text,
            active=target.active if active is None else active,
        )
        chunks = tuple(
            edited if chunk.chunk_id == target.chunk_id else chunk for chunk in self.corpus.chunks
        )
        return FixtureCorpus(self.corpus.documents, chunks)

    def _manifest(self, corpus: FixtureCorpus) -> dict[str, object]:
        return component_manifest(
            retriever=baseline_config(k=5),
            embedding_model=corpus.embedding_model,
            reranker=None,
            abstain_threshold=0.5,
            min_recall=0.6,
            min_citation_precision=0.8,
            min_abstention=0.9,
            min_grounding=0.9,
            corpus_chunks=len(corpus.chunks),
            corpus_digest=corpus_digest(corpus),
        )

    def test_a_chunk_text_edit_changes_the_digest(self) -> None:
        edited = self._edited_first_chunk(text=self.corpus.chunks[0].text + " (revised)")
        self.assertNotEqual(corpus_digest(self.corpus), corpus_digest(edited))

    def test_an_active_flag_flip_changes_the_digest(self) -> None:
        flipped = self._edited_first_chunk(active=not self.corpus.chunks[0].active)
        self.assertNotEqual(corpus_digest(self.corpus), corpus_digest(flipped))

    def test_a_content_edit_moves_the_run_id_waivers_are_pinned_to(self) -> None:
        flipped = self._edited_first_chunk(active=not self.corpus.chunks[0].active)
        self.assertNotEqual(run_id(self._manifest(self.corpus)), run_id(self._manifest(flipped)))

    def test_chunk_order_does_not_change_the_digest(self) -> None:
        reordered = FixtureCorpus(self.corpus.documents, tuple(reversed(self.corpus.chunks)))
        self.assertEqual(corpus_digest(self.corpus), corpus_digest(reordered))


class TestCorpusDigestCrossProcess(unittest.TestCase):
    def test_the_digest_is_identical_across_fresh_interpreters_and_hash_seeds(self) -> None:
        digests: list[str] = []
        uv = shutil.which("uv")
        assert uv is not None, "the eval toolchain runs under uv"
        for seed in ("1", "42"):
            result = subprocess.run(  # noqa: S603
                [uv, "run", "--frozen", "python", "-c", _DIGEST_SNIPPET],
                # The toolchain is the pinned `uv run --frozen` and the
                # arguments are constants; the seed is the whole point.
                env={**os.environ, "PYTHONHASHSEED": seed},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            digests.append(result.stdout.strip())
        self.assertEqual(digests[0], digests[1])
        self.assertRegex(digests[0], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
