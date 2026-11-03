"""terraform/aws: every instance requires IMDSv2 tokens with a hop limit of 1.
Usage billing (docs/REFERENCE.md "Telemetry") reads the instance's type and
region from IMDS on the host; the hop limit keeps machines on the workers out
of IMDS (one hop more than the host). Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import glob
import os
import re
import unittest

from test_machine_sizes import ROOT


def body(text: str, start: int) -> str:
    """The text between the `{` that ends at `start` and its closing brace."""
    depth, i = 1, start
    while depth and i < len(text):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[start:i - 1]


def blocks(text: str, name: str) -> list[str]:
    """The bodies of every `name { ... }` block, braces balanced."""
    return [body(text, m.end()) for m in re.finditer(rf"\b{name}\s*\{{", text)]


class MetadataOptions(unittest.TestCase):
    def test_every_aws_instance_requires_tokens_with_hop_limit_1(self):
        instances = 0
        for path in glob.glob(os.path.join(ROOT, "terraform", "aws", "*.tf")):
            with open(path) as fh:
                text = fh.read()
            for res in re.finditer(r'resource\s+"aws_instance"\s+"(\w+)"\s*\{', text):
                instances += 1
                (opts,) = blocks(body(text, res.end()), "metadata_options")
                settings = dict(re.findall(r'(\w+)\s*=\s*"?([\w-]+)"?', opts))
                self.assertEqual(settings.get("http_endpoint"), "enabled", res.group(1))
                self.assertEqual(settings.get("http_tokens"), "required", res.group(1))
                self.assertEqual(settings.get("http_put_response_hop_limit"), "1", res.group(1))
            # No launch template or other resource relaxes them.
            for opts in blocks(text, "metadata_options"):
                self.assertNotIn("optional", opts, path)
        self.assertGreater(instances, 0, "no aws_instance found: the test reads the wrong files")

    def test_the_block_reader(self):
        text = 'resource "aws_instance" "x" {\n  metadata_options {\n    http_tokens = "optional"\n  }\n}\n'
        (body,) = blocks(text, "metadata_options")
        self.assertIn('"optional"', body)


if __name__ == "__main__":
    unittest.main()
