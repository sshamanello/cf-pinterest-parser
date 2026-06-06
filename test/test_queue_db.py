import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cf_pinterest.queue_service import (
    export_n8n_ready_csv,
    export_n8n_ready_json,
    get_db_health,
    get_export_destinations,
    get_export_profiles,
    get_queue_stats,
    import_sheet_file_to_queue_db,
    prune_queue,
    rebuild_publish_queue,
    resolve_export_destination,
    sync_report_to_queue_db,
)


class QueueDbTestCase(unittest.TestCase):
    def test_export_profiles_loaded(self) -> None:
        profiles = get_export_profiles()
        self.assertIn("n8n_default", profiles)
        self.assertIn("n8n_minimal", profiles)
        self.assertIn("title", profiles["n8n_default"])

    def test_export_destinations_and_resolution(self) -> None:
        destinations = get_export_destinations()
        self.assertIn("remote", destinations)
        self.assertIn("local", destinations)

        out_remote, profile_remote = resolve_export_destination(
            destination="remote",
            niche="fonts",
            export_format="csv",
        )
        self.assertEqual(profile_remote, "n8n_default")
        self.assertTrue(out_remote.endswith("output/n8n/remote/fonts_publish.csv"))

        out_local_json, profile_local = resolve_export_destination(
            destination="local",
            niche="fonts",
            export_format="json",
        )
        self.assertEqual(profile_local, "n8n_default")
        self.assertTrue(out_local_json.endswith("output/n8n/local/fonts_publish.json"))

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
                                "public_image_url": "http://example.invalid/pins/ready/alpha.jpg",
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
            self.assertEqual(pub_row[3], "http://example.invalid/pins/ready/alpha.jpg")
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

    def test_db_health_pass_and_orphan_detection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cf_queue_health_") as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.db"
            csv_path = tmp_path / "queue.csv"
            csv_path.write_text(
                "slug,title,status,pin_jpg,affiliate_url\n"
                "theta,Theta Font,generated,/tmp/theta.jpg,https://example.com/theta\n",
                encoding="utf-8",
            )
            import_sheet_file_to_queue_db(file_path=csv_path, db_path=db_path, niche="fonts")

            health_ok = get_db_health(db_path)
            self.assertTrue(health_ok["ok"])
            self.assertEqual(health_ok["stats"]["publish_orphans"], 0)

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                INSERT OR REPLACE INTO publish_items (
                    slug, niche, title, description, image_url, target_url, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "orphan-slug",
                    "fonts",
                    "Orphan",
                    "orphan",
                    "x",
                    "y",
                    "generated",
                ),
            )
            conn.commit()
            conn.close()

            health_bad = get_db_health(db_path)
            self.assertFalse(health_bad["ok"])
            self.assertGreater(health_bad["stats"]["publish_orphans"], 0)

    def test_queue_stats_scope_and_recent_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cf_queue_stats_") as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.db"

            csv_fonts = tmp_path / "fonts.csv"
            csv_fonts.write_text(
                "slug,title,status,pin_jpg,affiliate_url\n"
                "f1,Font One,generated,/tmp/f1.jpg,https://example.com/f1\n"
                "f2,Font Two,uploaded,/tmp/f2.jpg,https://example.com/f2\n",
                encoding="utf-8",
            )
            import_sheet_file_to_queue_db(file_path=csv_fonts, db_path=db_path, niche="fonts")

            csv_graphics = tmp_path / "graphics.csv"
            csv_graphics.write_text(
                "slug,title,status,pin_jpg,affiliate_url\n"
                "g1,Graphic One,rejected,/tmp/g1.jpg,https://example.com/g1\n",
                encoding="utf-8",
            )
            import_sheet_file_to_queue_db(file_path=csv_graphics, db_path=db_path, niche="graphics")

            fonts_stats = get_queue_stats(db_path=db_path, niche="fonts", runs_limit=5)
            self.assertEqual(fonts_stats["total_items"], 2)
            self.assertEqual(fonts_stats["status_counts"].get("generated"), 1)
            self.assertEqual(fonts_stats["status_counts"].get("uploaded"), 1)
            self.assertGreaterEqual(len(fonts_stats["recent_runs"]), 1)

            all_stats = get_queue_stats(db_path=db_path, niche=None, runs_limit=5)
            self.assertEqual(all_stats["total_items"], 3)
            self.assertEqual(all_stats["niche_counts"].get("fonts"), 2)
            self.assertEqual(all_stats["niche_counts"].get("graphics"), 1)
            self.assertGreaterEqual(len(all_stats["recent_runs"]), 2)

    def test_rebuild_publish_queue(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cf_queue_rebuild_") as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.db"
            csv_path = tmp_path / "queue.csv"
            csv_path.write_text(
                "slug,title,status,pin_jpg,affiliate_url\n"
                "x1,Font X1,generated,/tmp/x1.jpg,https://example.com/x1\n"
                "x2,Font X2,uploaded,/tmp/x2.jpg,https://example.com/x2\n",
                encoding="utf-8",
            )
            import_sheet_file_to_queue_db(file_path=csv_path, db_path=db_path, niche="fonts")

            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM publish_items")
            conn.commit()
            conn.close()

            result_single = rebuild_publish_queue(db_path=db_path, niche="fonts")
            self.assertEqual(result_single["mode"], "single")
            self.assertEqual(result_single["rebuilt_rows"], 2)

            conn = sqlite3.connect(db_path)
            total = conn.execute("SELECT COUNT(*) FROM publish_items").fetchone()[0]
            conn.close()
            self.assertEqual(total, 2)

            result_all = rebuild_publish_queue(db_path=db_path, niche=None)
            self.assertEqual(result_all["mode"], "all")
            self.assertGreaterEqual(result_all["rebuilt_rows"], 2)

    def test_prune_queue_dry_run_and_apply(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cf_queue_prune_") as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.db"
            csv_path = tmp_path / "queue.csv"
            csv_path.write_text(
                "slug,title,status,pin_jpg,affiliate_url\n"
                "p1,Item P1,generated,/tmp/p1.jpg,https://example.com/p1\n",
                encoding="utf-8",
            )

            import_sheet_file_to_queue_db(file_path=csv_path, db_path=db_path, niche="fonts")
            import_sheet_file_to_queue_db(file_path=csv_path, db_path=db_path, niche="fonts")
            import_sheet_file_to_queue_db(file_path=csv_path, db_path=db_path, niche="fonts")

            conn = sqlite3.connect(db_path)
            before_runs = conn.execute("SELECT COUNT(*) FROM queue_sync_runs").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(before_runs, 3)

            dry = prune_queue(db_path=db_path, keep_sync_runs=1, apply_changes=False)
            self.assertFalse(dry["apply_changes"])
            self.assertGreaterEqual(dry["sync_runs_to_delete"], 2)

            conn = sqlite3.connect(db_path)
            still_runs = conn.execute("SELECT COUNT(*) FROM queue_sync_runs").fetchone()[0]
            conn.close()
            self.assertEqual(still_runs, before_runs)

            applied = prune_queue(db_path=db_path, keep_sync_runs=1, apply_changes=True)
            self.assertTrue(applied["apply_changes"])
            self.assertGreaterEqual(applied["sync_runs_to_delete"], 2)

            conn = sqlite3.connect(db_path)
            after_runs = conn.execute("SELECT COUNT(*) FROM queue_sync_runs").fetchone()[0]
            conn.close()
            self.assertLessEqual(after_runs, 1)


if __name__ == "__main__":
    unittest.main()
