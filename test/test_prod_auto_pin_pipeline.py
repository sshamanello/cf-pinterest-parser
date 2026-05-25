import json
import tempfile
import unittest
from pathlib import Path

from prod_auto_pin_pipeline import _load_products_file, _prepare_local_preview


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

    def test_load_products_file_accepts_local_image_path_without_image_url(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cf_products_local_") as tmp:
            tmp_path = Path(tmp)
            local_image = tmp_path / "local.png"
            local_image.write_bytes(b"fake")

            products_path = tmp_path / "products_local.json"
            products_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {"slug": "ok-local", "local_image_path": str(local_image), "title": "Local"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            items = _load_products_file(products_path, niche="fonts")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["slug"], "ok-local")
            self.assertEqual(items[0]["local_image_path"], str(local_image))

    def test_prepare_local_preview_copies_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cf_prepare_local_preview_") as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "src.jpg"
            src.write_bytes(b"binary")
            input_dir = tmp_path / "input"
            input_dir.mkdir(parents=True, exist_ok=True)

            product = {"slug": "sample", "local_image_path": str(src)}
            dst = _prepare_local_preview(product, input_dir)
            self.assertIsNotNone(dst)
            self.assertTrue(dst.exists())
            self.assertEqual(dst.read_bytes(), b"binary")


if __name__ == "__main__":
    unittest.main()
