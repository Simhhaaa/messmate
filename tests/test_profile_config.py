from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from messmate_nutrition.profile_config import ProfileConfigurationError, load_user_profile


class PrivateProfileConfigurationTests(unittest.TestCase):
    def test_process_environment_loads_profile_and_preserves_targets(self):
        profile = load_user_profile(environ={
            "MESSMATE_USER_HEIGHT_CM": "171",
            "MESSMATE_USER_WEIGHT_KG": "66.5",
            "MESSMATE_USER_GOAL": "Synthetic test goal",
        }, private_file=Path("missing-profile-file"))
        self.assertEqual((profile.height_cm, profile.weight_kg), (Decimal("171"), Decimal("66.5")))
        self.assertEqual(profile.goal, "Synthetic test goal")
        self.assertEqual(profile.targets.preferred_calories_kcal, Decimal("1900"))
        self.assertEqual(profile.targets.minimum_calories_kcal, Decimal("1800"))
        self.assertEqual(profile.targets.maximum_calories_kcal, Decimal("2000"))

    def test_private_file_is_used_but_environment_takes_precedence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text(
                "MESSMATE_USER_HEIGHT_CM=172\nMESSMATE_USER_WEIGHT_KG=67\n"
                "MESSMATE_USER_GOAL=Local synthetic goal\n", encoding="utf-8"
            )
            profile = load_user_profile(
                environ={"MESSMATE_USER_HEIGHT_CM": "173"}, private_file=path
            )
            self.assertEqual(profile.height_cm, Decimal("173"))
            self.assertEqual(profile.weight_kg, Decimal("67"))
            self.assertEqual(profile.goal, "Local synthetic goal")

    def test_missing_values_fail_clearly_without_values(self):
        with self.assertRaises(ProfileConfigurationError) as raised:
            load_user_profile(environ={}, private_file=Path("missing-profile-file"))
        message = str(raised.exception)
        self.assertIn("MESSMATE_USER_HEIGHT_CM", message)
        self.assertIn("MESSMATE_USER_WEIGHT_KG", message)
        self.assertIn("MESSMATE_USER_GOAL", message)

    def test_malformed_and_out_of_range_values_fail_safely(self):
        base = {
            "MESSMATE_USER_HEIGHT_CM": "not-a-number",
            "MESSMATE_USER_WEIGHT_KG": "65",
            "MESSMATE_USER_GOAL": "Synthetic goal",
        }
        with self.assertRaisesRegex(ProfileConfigurationError, "decimal numbers"):
            load_user_profile(environ=base, private_file=Path("missing-profile-file"))
        base["MESSMATE_USER_HEIGHT_CM"] = "999"
        with self.assertRaisesRegex(ProfileConfigurationError, "height_cm"):
            load_user_profile(environ=base, private_file=Path("missing-profile-file"))


if __name__ == "__main__":
    unittest.main()
