from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline.glossary.glossary import load_glossary


class GlossaryTests(unittest.TestCase):
    def test_bom_header_and_quoted_commas(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "glossary.csv"
            path.write_text(
                '\ufeff"source","target"\n'
                '"曝光, 自动","экспозиция, автоматическая"\n'
                '软件,"ПО, программное обеспечение"\n',
                encoding="utf-8",
            )

            self.assertEqual(
                {
                    "曝光, 自动": "экспозиция, автоматическая",
                    "软件": "ПО, программное обеспечение",
                },
                load_glossary(path),
            )

    def test_header_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "glossary.csv"
            path.write_text("图,рис.\n表,табл.\n", encoding="utf-8")

            self.assertEqual({"图": "рис.", "表": "табл."}, load_glossary(path))


if __name__ == "__main__":
    unittest.main()
