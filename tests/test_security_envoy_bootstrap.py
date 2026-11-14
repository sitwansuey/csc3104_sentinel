"""Every generated Envoy bootstrap of the security lab loads in the pinned Envoy (P5.06 file-based SDS).

Live on 14 Nov 2026 every sidecar crash-looped with "TlsCertificateSdsApi: node 'id' and 'cluster' are
required": a bootstrap with an SDS secret source must name its node, and the offline tests never started an
Envoy. The first test checks the generated files; the second runs the pinned Envoy image in --mode validate
when Docker and that image are present on this machine, and is skipped otherwise.
"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.security import boutique_security_manifest as manifest
from scripts.security import security_lab


def bootstraps():
    """(name, files) for every Envoy configuration the labs generate."""
    configs = {"security-gateway": manifest.gateway_config(),
               **{"security-" + route + "-upstream": manifest.proxy_config(upstream=True, route=route)
                  for route in manifest.ROUTES},
               **{"security-" + caller + "-egress": manifest.caller_config(caller) for caller in manifest.CALLERS},
               "compose-gateway": security_lab.envoy_config(),
               "compose-upstream": security_lab.envoy_config(upstream=True)}
    return [(name, security_lab.config_files(config, node=name)) for name, config in sorted(configs.items())]


def envoy_available():
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "image", "inspect", security_lab.ENVOY], capture_output=True, timeout=30,
                                check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class EnvoyBootstrapTests(unittest.TestCase):
    def test_every_bootstrap_names_its_node(self):
        for name, files in bootstraps():
            with self.subTest(name=name):
                node = json.loads(files["envoy.json"])["node"]
                self.assertTrue(node["id"] and node["cluster"])

    @unittest.skipUnless(envoy_available(), "Docker or the pinned Envoy image is not available here")
    def test_every_bootstrap_loads_in_the_pinned_envoy(self):
        for name, files in bootstraps():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                for file, text in files.items():
                    (Path(folder) / file).write_text(text, encoding="utf-8")
                result = subprocess.run(["docker", "run", "--rm", "-v", f"{Path(folder).resolve()}:/config:ro",
                                         security_lab.ENVOY, "--mode", "validate", "-c", "/config/envoy.json"],
                                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                                        timeout=120, check=False)
                self.assertEqual(result.returncode, 0, result.stdout[-600:] + result.stderr[-600:])


if __name__ == "__main__":
    unittest.main()
