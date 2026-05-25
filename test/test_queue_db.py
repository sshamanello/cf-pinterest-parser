import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cf_pinterest.queue_service import (
    export_n8n_ready_csv,
    export_n8n_ready_json,
    import_sheet_file_to_queue_db,
    sync_report_to_queue_db,
)


class QueueDbTestCase(unittest.TestCase):
    def test_sync_report_to_queue_db_upsert(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cf_queue_db_") as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.db"
            report_path = tmp_path / "report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "slug": "alpha",
                                "status": "generated",
                                "title": "Alpha Font",
                                "pin_jpg": "/tmp/a.jpg",
                                "cf_url": "https://example.com/a",
                                "affiliate_url": "https://example.com/a/ref",
                                "image_url": "https://example.com/a.png",
                                "source_file": "/tmp/alpha.jpg",
                            },
                            {
                                "slug": "alpha",
                                "status": "uploaded",
                                "title": "Alpha Font v2",
                                "pin_jpg": "/tmp/a2.jpg",
                                "cf_url": "https://example.com/a",
                                "affiliate_url": "https://example.com/a/ref",
                                "image_url": "https://example.com/a2.png",
                                "source_file": "/tmp/alpha.jpg",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            synced = sync_report_to_queue_db(report_path=report_path, db_path=db_path, niche="fonts")
            self.assertEqual(synced.parsed_items, 2)
            self.assertEqual(synced.upserted_items, 2)
            self.assertEqual(synced.uploaded_items, 1)

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT slug, title, status, niche FROM queue_items WHERE slug = 'alpha'").fetchone()
            pub_row = conn.execute(
                "SELECT slug, title, description, image_url, target_url, status FROM publish_items WHERE slug = 'alpha'"
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertIsNotNone(pub_row)
            self.assertEqual(row[0], "alpha")
            self.assertEqual(row[1], "Alpha Font v2")
            self.assertEqual(row[2], "uploaded")
            self.assertEqual(row[3], "fonts")
            self.assertEqual(pub_row[1], "Alpha Font v2")
            self.assertEqual(pub_row[5], "uploaded")

    def test_import_csv_and_export_n8n(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cf_queue_csv_") as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.db"
            csv_path = tmp_path / "queue.csv"
            csv_path.write_text(
                "slug,title,status,pin_jpg,affiliate_url\n"
                "beta,Beta Font,generated,/tmp/beta.jpg,https://example.com/beta\n"
                "gamma,Gamma Font,rejected,/tmp/gamma.jpg,https://example.com/gamma\n",
                encoding="utf-8",
            )

            imported = import_sheet_file_to_queue_db(file_path=csv_path, db_path=db_path, niche="fonts")
            self.assertEqual(imported.parsed_items, 2)
            self.assertEqual(imported.upserted_items, 2)
            self.assertEqual(imported.generated_items, 1)
            self.assertEqual(imported.rejected_items, 1)

            export_path = tmp_path / "n8n.csv"
            exported = export_n8n_ready_csv(db_path=db_path, niche="fonts", output_path=export_path, limit=100)
            self.assertEqual(exported, 1)
            raw = export_path.read_text(encoding="utf-8")
            self.assertIn("beta", raw)
            self.assertNotIn("gamma", raw)

            export_path_min = tmp_path / "n8n_minimal.csv"
            exported_min = export_n8n_ready_csv(
                db_path=db_path,
                niche="fonts",
                output_path=export_path_min,
                limit=100,
                profile="n8n_minimal",
                statuses=(),
            )
            self.assertEqual(exported_min, 2)
            raw_min = export_path_min.read_text(encoding="utf-8")
            self.assertIn("title,description,image_url,target_url", raw_min.splitlines()[0])
            self.assertIn("Beta Font", raw_min)
            self.assertIn("Gamma Font", raw_min)

            export_path_json = tmp_path / "n8n_default.json"
            exported_json = export_n8n_ready_json(
                db_path=db_path,
                niche="fonts",
                output_path=export_path_json,
                limit=100,
                profile="n8n_default",
                statuses=("generated",),
            )
            self.assertEqual(exported_json, 1)
            payload = json.loads(export_path_json.read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["title"], "Beta Font")
            self.assertEqual(payload[0]["status"], "generated")


if __name__ == "__main__":
    unittest.main()
