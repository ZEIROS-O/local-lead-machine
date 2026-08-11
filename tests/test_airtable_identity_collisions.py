from __future__ import annotations

import unittest

from lead_machine.airtable_sync import plan_airtable_sync


class AirtableIdentityCollisionTests(unittest.TestCase):
    def test_refuses_distinct_input_keys_that_resolve_to_one_existing_phone_record(self) -> None:
        authoritative = {
            "upsert_key": "place:authoritative-sbr",
            "phone": "(216) 555-0100",
            "airtable_fields": {
                "Business Name": "SBR Concrete Polishing INC",
                "Phone": "(216) 555-0100",
            },
        }
        stale = {
            "upsert_key": "place:backfill:sbr-concrete-polishing-inc:cleveland",
            "phone": "(216) 555-0100",
            "airtable_fields": {
                "Business Name": "SBR Concrete Polishing INC",
                "Phone": "(216) 555-0100",
            },
        }

        with self.assertRaisesRegex(
            ValueError,
            "Airtable existing record rec-sbr matched distinct input upsert keys",
        ):
            plan_airtable_sync(
                [authoritative, stale],
                existing_records_by_key={"phone:2165550100": "rec-sbr"},
            )


if __name__ == "__main__":
    unittest.main()
