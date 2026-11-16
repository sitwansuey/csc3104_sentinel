"""The submission readiness gate, with fake git and ffprobe. Nothing reaches GitHub or a real tool."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.instructions import build_pdf
from scripts.submission import check

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
TAG = "v1.0.0"
MANUAL = [{"id": "M-AI-POLICY", "text": "Read the course's generative-AI policy"},
          {"id": "M-UPLOAD", "text": "Upload to the course system"}]


def fake_pdf(pages, version=TAG, extra=b""):
    body = b"".join(b"%d 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n" % (10 + n) for n in range(pages))
    return (b"%PDF-1.4\n2 0 obj\n<< /Type /Pages /Count " + str(pages).encode() + b" >>\nendobj\n" + body +
            b"7 0 obj\n<< /SentinelVersion (" + version.encode() + b") >>\nendobj\n" + extra + b"%%EOF\n")


class FakeTools:
    """Answers git rev-parse, git ls-remote and ffprobe; records every call."""

    def __init__(self, tags=None, remote_tags=None, probe=None, ffprobe_missing=False):
        self.tags = {TAG: COMMIT} if tags is None else tags
        self.remote_tags = set(self.tags) if remote_tags is None else remote_tags
        self.probe = probe
        self.ffprobe_missing = ffprobe_missing
        self.calls = []

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        if args[0] == "git" and "rev-parse" in args:
            tag = args[-1].removeprefix("refs/tags/").removesuffix("^{commit}")
            return (0, self.tags[tag] + "\n") if tag in self.tags else (1, "")
        if args[0] == "git" and "ls-remote" in args:
            tag = args[-1].removeprefix("refs/tags/")
            return (0, f"{COMMIT}\trefs/tags/{tag}\n") if tag in self.remote_tags else (0, "")
        if args[0] == "ffprobe":
            if self.ffprobe_missing:
                return 127, ""
            return (0, json.dumps(self.probe)) if self.probe is not None else (1, "")
        raise AssertionError(f"unexpected command {args}")


def probe(minutes=13.5, audio=True, height=1080):
    streams = [{"codec_type": "video", "height": height}] + ([{"codec_type": "audio"}] if audio else [])
    return {"format": {"duration": str(minutes * 60)}, "streams": streams}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "docs/submission/rehearsals").mkdir(parents=True)
        (self.root / "dist").mkdir()
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"not really a video")

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, **changes):
        data = {"schema": check.SCHEMA, "release_tag": TAG, "instructions_pdf": "dist/GROUP05_INSTRUCTIONS.pdf",
                "article_pdf": "article/article.pdf", "article_max_pages": 6,
                "video_link": "https://video.example.invalid/watch/1", "video_seconds": {"min": 600, "max": 900},
                "rehearsal_tiers": ["linux-container", "teammate-laptop"],
                "rehearsal_records": ["docs/submission/rehearsals/linux.json",
                                      "docs/submission/rehearsals/laptop.json"],
                "manual": MANUAL}
        data.update(changes)
        (self.root / check.MANIFEST).write_text(json.dumps(data), encoding="utf-8")

    def record(self, name, tier, outcome="PASS", revision=COMMIT):
        (self.root / "docs/submission/rehearsals" / name).write_text(json.dumps(
            {"schema": check.REHEARSAL_SCHEMA, "tier": tier, "outcome": outcome, "source_revision": revision}),
            encoding="utf-8")

    def complete(self):
        """Every deliverable present and valid."""
        self.manifest()
        (self.root / "dist/GROUP05_INSTRUCTIONS.pdf").write_bytes(fake_pdf(9))
        (self.root / "article").mkdir(exist_ok=True)
        (self.root / "article/article.pdf").write_bytes(fake_pdf(6, version="article"))
        self.record("linux.json", "linux-container")
        self.record("laptop.json", "teammate-laptop")

    def results(self, tools=None, video=None, remote=False):
        tools = tools or FakeTools(probe=probe())
        return {result.check: result for result in check.evaluate(self.root, video, remote, tools)}

    def status(self, name, **options):
        return self.results(**options)[name].status

    def run_cli(self, *args, tools=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check.main(["--root", str(self.root), *args], run=tools or FakeTools(probe=probe()))
        return code, out.getvalue() + err.getvalue()

    def test_everything_present_exits_zero_and_manual_items_stay_manual(self):
        self.complete()
        code, output = self.run_cli("--video-file", str(self.video), "--remote")
        self.assertEqual(code, 0, output)
        results = self.results(video=self.video, remote=True)
        automated = [result for name, result in results.items() if not name.startswith("M-")]
        self.assertTrue(all(result.status == "PASS" for result in automated),
                        [(r.check, r.status, r.detail) for r in automated])
        self.assertEqual([results[item["id"]].status for item in MANUAL], ["MANUAL", "MANUAL"])
        self.assertIn("2 item(s) need a person (MANUAL never passes automatically)", output)

    def test_the_empty_starting_manifest_fails_and_lists_every_deliverable(self):
        self.manifest(release_tag=None, article_pdf=None, video_link=None, rehearsal_records=[])
        code, output = self.run_cli()
        self.assertEqual(code, 1)
        for name in ("release-tag", "instructions-pdf", "article-pdf", "video-link", "rehearsal:linux-container",
                     "rehearsal:teammate-laptop"):
            self.assertRegex(output, rf"FAIL\s+{name}\s")
        self.assertIn("NOT READY", output)

    def test_the_committed_manifest_is_valid_and_not_ready(self):
        manifest = check.load_manifest(ROOT)
        self.assertIn("M-AI-POLICY", [item["id"] for item in manifest["manual"]])
        self.assertEqual(manifest["rehearsal_tiers"], list(check.TIERS))
        results = check.evaluate(ROOT, run=FakeTools(tags={}))
        self.assertTrue(any(result.status == "FAIL" for result in results))

    def test_missing_or_unknown_tag_fails(self):
        self.complete()
        self.assertEqual(self.status("release-tag", tools=FakeTools(tags={})), "FAIL")
        self.assertEqual(self.status("release-tag", tools=FakeTools(remote_tags=set()), remote=True), "FAIL")
        self.assertEqual(self.status("release-tag", tools=FakeTools(remote_tags=set())), "PASS")

    def test_instructions_pdf_problems_fail(self):
        self.complete()
        target = self.root / "dist/GROUP05_INSTRUCTIONS.pdf"
        cases = {
            "renamed": lambda: self.manifest(instructions_pdf="dist/group05_instructions.pdf"),
            "not a pdf": lambda: target.write_bytes(b"<html>"),
            "no pages": lambda: target.write_bytes(fake_pdf(0)),
            "placeholder": lambda: target.write_bytes(fake_pdf(3, extra=build_pdf.PLACEHOLDER.encode())),
            "other version": lambda: target.write_bytes(fake_pdf(3, version="v0.9.0")),
            "missing": target.unlink,
        }
        for label, breaker in cases.items():
            with self.subTest(label):
                self.complete()
                breaker()
                self.assertEqual(self.status("instructions-pdf"), "FAIL")

    def test_a_pdf_built_by_build_pdf_passes(self):
        self.complete()
        from tests.test_instructions_source import Fixture

        fixture = Fixture()
        try:
            build_pdf.build(fixture.root, self.root / "dist", version_text=TAG, revision_text="x",
                            repository_url="https://example.invalid", members="A, B")
        finally:
            fixture.close()
        self.assertEqual(self.status("instructions-pdf"), "PASS")

    def test_article_page_limit(self):
        self.complete()
        (self.root / "article/article.pdf").write_bytes(fake_pdf(7))
        self.assertEqual(self.status("article-pages"), "FAIL")
        (self.root / "article/article.pdf").write_bytes(b"%PDF-1.7\n compressed objects only %%EOF")
        self.assertEqual(self.status("article-pages"), "MANUAL")
        self.manifest(article_max_pages=None)
        self.assertEqual(self.status("article-pages"), "MANUAL")
        self.manifest(article_pdf="article/missing.pdf")
        self.assertEqual(self.status("article-pdf"), "FAIL")

    def test_video_link_must_be_https(self):
        self.complete()
        self.manifest(video_link="http://video.example.invalid/1")
        self.assertEqual(self.status("video-link"), "FAIL")
        self.manifest(video_link="")
        self.assertEqual(self.status("video-link"), "FAIL")

    def test_video_file_duration_audio_and_resolution(self):
        self.complete()
        cases = {(9.5, True, 1080): "FAIL", (16, True, 1080): "FAIL", (13.5, False, 1080): "FAIL",
                 (13.5, True, 720): "FAIL", (10, True, 1080): "PASS", (15, True, 2160): "PASS"}
        for (minutes, audio, height), expected in cases.items():
            with self.subTest(minutes=minutes, audio=audio, height=height):
                tools = FakeTools(probe=probe(minutes, audio, height))
                self.assertEqual(self.status("video-file", tools=tools, video=self.video), expected)
        self.assertEqual(self.status("video-file"), "MANUAL")
        self.assertEqual(self.status("video-file", tools=FakeTools(ffprobe_missing=True), video=self.video), "MANUAL")
        self.assertEqual(self.status("video-file", tools=FakeTools(probe=None), video=self.video), "FAIL")
        self.assertEqual(self.status("video-file", video=self.root / "absent.mp4"), "FAIL")

    def test_rehearsal_records_must_pass_on_the_tag_commit(self):
        self.complete()
        self.record("linux.json", "linux-container", revision="b" * 40)
        self.assertEqual(self.status("rehearsal:linux-container"), "FAIL")
        self.record("linux.json", "linux-container", outcome="FAIL")
        self.assertEqual(self.status("rehearsal:linux-container"), "FAIL")
        self.record("linux.json", "linux-container")
        self.assertEqual(self.status("rehearsal:linux-container", tools=FakeTools(tags={})), "FAIL")
        (self.root / "docs/submission/rehearsals/laptop.json").unlink()
        results = self.results()
        self.assertEqual(results["rehearsal:teammate-laptop"].status, "FAIL")
        self.assertEqual(results["rehearsal-records"].status, "FAIL")

    def test_manual_items_never_pass_even_when_everything_else_does(self):
        self.complete()
        manual = [result for result in check.evaluate(self.root, self.video, False, FakeTools(probe=probe()))
                  if result.check.startswith("M-")]
        self.assertEqual({result.status for result in manual}, {"MANUAL"})

    def test_bad_manifest_exits_two(self):
        (self.root / check.MANIFEST).write_text("{", encoding="utf-8")
        self.assertEqual(self.run_cli()[0], 2)
        self.manifest(rehearsal_tiers=["somewhere"])
        self.assertEqual(self.run_cli()[0], 2)
        self.manifest(manual=[{"id": "M-X"}])
        self.assertEqual(self.run_cli()[0], 2)
        (self.root / check.MANIFEST).unlink()
        self.assertEqual(self.run_cli()[0], 2)

    def test_markdown_output_is_a_table(self):
        self.complete()
        code, output = self.run_cli("--format", "markdown")
        self.assertEqual(code, 0)
        self.assertIn("| Status | Check | Detail |", output)
        self.assertIn("| MANUAL | M-AI-POLICY |", output)

    def test_only_read_only_commands_are_run(self):
        self.complete()
        tools = FakeTools(probe=probe())
        check.evaluate(self.root, self.video, True, tools)
        for call in tools.calls:
            self.assertTrue(call[:4] == ["git", "-C", str(self.root), "rev-parse"]
                            or call[:4] == ["git", "-C", str(self.root), "ls-remote"] or call[0] == "ffprobe", call)


if __name__ == "__main__":
    unittest.main()
