"""Execute the production update SQL against SQLite: commit fencing and rollback.

This checks durable state transitions, not HarmonyOS/Asset/system scheduling.
"""
import pathlib
import re
import sqlite3
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "apps/harmonyos/entry/src/main/ets/update/UpdateDB.ets"


def statements():
    raw = SOURCE.read_text()
    return [sql.replace("\\'", "'").replace("\\\\", "\\")
            for sql in re.findall(r"executeSql\('((?:\\.|[^'\\])*)'", raw)]


class DurableUpdateTests(unittest.TestCase):
    def setUp(self):
        self.sql = statements()
        self.db = sqlite3.connect(":memory:")
        for sql in self.sql:
            if sql.startswith("CREATE TABLE") or sql.startswith("INSERT OR IGNORE"):
                self.db.execute(sql)
        self.activate = next(sql for sql in self.sql if "SET previous=active" in sql)
        self.rollback = next(sql for sql in self.sql if "SET active=previous" in sql)
        self.db.execute("UPDATE update_state SET active=1,active_json='signed-v1',highest=1,pending='signed-v2',lease='owner',lease_until=200")

    def tearDown(self):
        self.db.close()

    def activate_v2(self, owner="owner", now=100, raw="signed-v2"):
        self.db.execute(self.activate, [2, raw, 2, now, owner, now, raw, 2])

    def state(self):
        return self.db.execute("SELECT active,active_json,previous,previous_json,highest,pending FROM update_state").fetchone()

    def test_incomplete_or_stale_owner_cannot_switch_active_pointer(self):
        original = self.state()
        for args in ({"owner": "other"}, {"now": 201}, {"raw": "different-pending"}):
            self.activate_v2(**args)
            self.assertEqual(self.state(), original)

    def test_atomic_activation_then_local_rollback_retains_highest(self):
        self.activate_v2()
        self.assertEqual(self.state(), (2, "signed-v2", 1, "signed-v1", 2, ""))
        self.db.execute(self.rollback, ["owner", 100])
        self.assertEqual(self.state(), (1, "signed-v1", 0, "", 2, ""))

    def test_old_lease_cannot_rollback_new_owner(self):
        self.activate_v2()
        self.db.execute("UPDATE update_state SET lease='new-owner'")
        self.db.execute(self.rollback, ["owner", 100])
        self.assertEqual(self.state()[0], 2)

    def test_only_one_process_obtains_compare_and_set_lease(self):
        claim = next(sql for sql in self.sql if "SET lease=?,lease_until=?" in sql)
        self.db.execute(claim, ["first", 500, 300])
        self.db.execute(claim, ["second", 500, 300])
        self.assertEqual(self.db.execute("SELECT lease FROM update_state").fetchone()[0], "first")


if __name__ == "__main__":
    unittest.main()
