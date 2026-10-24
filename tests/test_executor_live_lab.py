"""Regression for actual kind import metadata; no cluster calls in these tests."""

import unittest

from scripts.test_executor_live import loaded_image_digest


class LoadedImageDigestTests(unittest.TestCase):
    def test_matching_repository_digest_is_retained(self):
        image = "docker.io/library/sentinel-demo@sha256:" + "a" * 64
        self.assertEqual(loaded_image_digest({"status": {"repoDigests": [image]}}, ""), image)

    def test_import_name_and_image_config_hash_do_not_replace_named_manifest(self):
        inspection = {"status": {"id": "sha256:" + "b" * 64,
                                  "repoDigests": ["docker.io/library/import-today@sha256:" + "c" * 64]}}
        listing = "REF TYPE DIGEST SIZE\n" + "docker.io/library/sentinel-demo:0.1.0 index sha256:" + "a" * 64 + " 41MiB\n"
        self.assertEqual(loaded_image_digest(inspection, listing), "docker.io/library/sentinel-demo@sha256:" + "a" * 64)

    def test_unrelated_or_invalid_image_entries_cannot_authorize_a_digest(self):
        for listing in ("another-image index sha256:" + "a" * 64,
                        "docker.io/library/sentinel-demo:0.1.0 index not-a-digest"):
            with self.subTest(listing=listing), self.assertRaises(RuntimeError):
                loaded_image_digest({"status": {"repoDigests": []}}, listing)
