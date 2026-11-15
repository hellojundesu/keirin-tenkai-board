import unittest
from meeting_history import build_meeting_history

class MeetingHistoryTests(unittest.TestCase):
    def test_as_of_and_meeting_isolation(self):
        rows = [
            {"meeting_id":"m1","registration_no":"123","race_date":"2026-11-14","day_label":"3日目","race_number":8,"comment":"三日目"},
            {"meeting_id":"m1","registration_no":"123","race_date":"2026-11-12","day_label":"初日","race_number":3,"comment":"初日"},
            {"meeting_id":"m1","registration_no":"123","race_date":"2026-11-15","race_number":9,"comment":"未来"},
            {"meeting_id":"m2","registration_no":"123","race_date":"2026-11-13","race_number":4,"comment":"別開催"},
            {"meeting_id":"m1","registration_no":"999","race_date":"2026-11-13","race_number":4,"comment":"別選手"},
        ]
        result=build_meeting_history(rows,meeting_id="m1",registration_no="123",as_of="2026-11-14")
        self.assertEqual([x["comment"] for x in result],["初日","三日目"])
    def test_conflict_rejected(self):
        rows=[{"meeting_id":"m","registration_no":"1","race_date":"2026-11-12","race_number":1,"comment":v} for v in ["A","B"]]
        with self.assertRaises(ValueError):
            build_meeting_history(rows,meeting_id="m",registration_no="1",as_of="2026-11-12")

if __name__=="__main__":
    unittest.main()
