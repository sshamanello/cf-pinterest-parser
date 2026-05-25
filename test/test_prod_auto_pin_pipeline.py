import json
import tempfile
import unittest
from pathlib import Path

from prod_auto_pin_pipeline import _load_products_file


class ProdAutoPinPipelineTestCase(unittest.TestCase):
    def test_load_products_file_filters_invalid_items(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cf_products_file_") as tmp:
            tmp_path = Path(tmp)
            products_path = tmp_path / "products.json"
            products_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {"slug": "ok-1", "image_url": "https://example.com/1.jpg", "title": "One"},
                            {"slug": "", "image_url": "https://example.com/2.jpg", "title": "No slug"},
                            {"slug": "no-image", "image_url": "", "title": "No image"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            items = _load_products_file(products_path, niche="fonts")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["slug"], "ok-1")
            self.assertEqual(items[0]["niche"], "fonts")


if __name__ == "__main__":
    unittest.main()
