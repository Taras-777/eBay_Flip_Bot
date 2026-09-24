import unittest
from unittest.mock import Mock, patch

import main


class RetryTests(unittest.TestCase):
    @patch("main.time.sleep")
    @patch("main.requests.request")
    def test_retries_rate_limit_and_honors_retry_after(self, request, sleep):
        first = Mock(status_code=429, headers={"Retry-After": "2"})
        second = Mock(status_code=200, headers={})
        request.side_effect = [first, second]

        response = main._request_with_retries("GET", "https://example.test")

        self.assertIs(response, second)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(2.0)

    @patch("main.time.sleep")
    @patch("main.requests.request")
    def test_retries_connection_error_then_returns_response(self, request, sleep):
        response = Mock(status_code=200, headers={})
        request.side_effect = [
            main.requests.exceptions.Timeout("timed out"),
            response,
        ]

        result = main._request_with_retries("GET", "https://example.test")

        self.assertIs(result, response)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(main.NETWORK_BACKOFF_SECONDS)

    @patch("main.time.sleep")
    @patch("main.requests.request")
    def test_returns_final_error_response_without_extra_retry(self, request, sleep):
        response = Mock(status_code=503, headers={})
        request.return_value = response

        result = main._request_with_retries("GET", "https://example.test")

        self.assertIs(result, response)
        self.assertEqual(request.call_count, main.NETWORK_MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, main.NETWORK_MAX_ATTEMPTS - 1)


class MarketLogicTests(unittest.TestCase):
    def test_title_matching_filters_console_accessories(self):
        self.assertTrue(main._title_matches_search("Sony PlayStation 5 Console", "ps 5", ""))
        self.assertTrue(main._title_matches_search("Sony PlayStation 5 Console", "ps5", ""))
        self.assertTrue(main._title_matches_search("Sony PS5 Console", "playstation 5", ""))
        self.assertTrue(main._title_matches_search("Sony PlayStation 5 Pro Console", "ps 5 pro", ""))
        self.assertTrue(main._title_matches_search("Sony PlayStation 5 Console", "play station 5", ""))
        self.assertFalse(main._title_matches_search("Sony PlayStation 5 Console", "ps 5 pro", ""))
        self.assertFalse(main._title_matches_search("PS5 Controller", "ps 5", ""))
        self.assertTrue(main._title_matches_search("Sony PlayStation 5 Digital Edition Console", "ps5", ""))
        self.assertTrue(main._title_matches_search("Sony PlayStation 5 825GB", "ps5", ""))
        self.assertTrue(main._title_matches_search("Sony PlayStation 5", "ps5", ""))
        self.assertFalse(main._title_matches_search("PlayStation 5 Digital Code", "ps5", ""))
        self.assertFalse(main._title_matches_search("PlayStation 5 Empty Box", "ps5", ""))
        self.assertFalse(
            main._title_matches_search(
                "Destruction AllStars Sony PlayStation 5 - PAL - USK 12",
                "ps5",
                "",
            )
        )
        self.assertFalse(
            main._title_matches_search(
                "PS5 / PlayStation 5 - The Plucky Squire Deluxe Edition - NEU / OVP",
                "playstation 5",
                "",
            )
        )
        self.assertFalse(main._title_matches_search("Nintendo Switch Game", "ps 5", ""))

    def test_title_matching_rejects_excluded_terms(self):
        self.assertFalse(main._title_matches_search("iPhone 12 Pro broken parts", "iphone 12 pro", "broken parts"))
        self.assertTrue(main._title_matches_search("Apple iPhone 13 128GB Black", "iPhone 13 128GB", ""))
        self.assertFalse(main._title_matches_search("Apple iPhone 13 Pro Max 128GB", "iPhone 13 128GB", ""))
        self.assertTrue(main._title_matches_search("Apple iPhone 13 Pro 128GB", "iPhone 13 Pro 128GB", ""))
        self.assertFalse(main._title_matches_search("Apple iPhone 13 Pro Max 128GB", "iPhone 13 Pro 128GB", ""))
        self.assertTrue(
            main._title_matches_search(
                "Dell Latitude 3400 Laptop i5-8265U 8GB RAM 256GB SSD",
                "dell latitude 3400",
                "",
            )
        )
        self.assertTrue(
            main._title_matches_search(
                "Dell Latitude 3400 i5-8265U 8GB RAM 256GB SSD USB HDMI",
                "dell latitude 3400",
                "",
            )
        )
        self.assertTrue(
            main._title_matches_search(
                "Dell Latitude 5230 2-in-1 8GB RAM 256GB SSD SD Card Reader",
                "dell latitude 5230",
                "",
            )
        )
        self.assertFalse(
            main._title_matches_search(
                "Dell Latitude 3400 Touchpad Kabel",
                "dell latitude 3400",
                "",
            )
        )
        self.assertFalse(
            main._title_matches_search(
                "Dell Latitude 3400 USB SD I/O Karte",
                "dell latitude 3400",
                "",
            )
        )
        self.assertFalse(
            main._title_matches_search(
                "Dell Latitude 3400 Motherboard 8GB RAM",
                "dell latitude 3400",
                "",
            )
        )
        self.assertFalse(
            main._title_matches_search(
                "Dell Latitude 3400 Laptop Netzteil Ladegerät",
                "dell latitude 3400",
                "",
            )
        )
        self.assertTrue(
            main._title_matches_search(
                "Dell Latitude 3400 Laptop i5-8265U 8GB RAM 256GB SSD mit Ladegerät",
                "dell latitude 3400",
                "",
            )
        )

    def test_median_empty_input(self):
        self.assertIsNone(main.compute_median([]))

    def test_median_returns_numeric_value(self):
        self.assertEqual(main.compute_median([10, 10, 20, 20, 20, 30, 30, 30]), 20)

    def test_specific_model_can_use_three_listings(self):
        self.assertEqual(main.minimum_sample_size_for_query("Dell Latitude 5230"), 3)
        self.assertEqual(main.compute_median([100, 120, 140], min_sample_size=3), 120)

    def test_foreign_currency_rates_are_available_for_euro_median(self):
        self.assertAlmostEqual(84.99 * main.CURRENCY_TO_EUR["GBP"], 99.44, places=2)

    def test_filter_outliers_preserves_small_samples(self):
        prices = [10, 20, 100]
        self.assertEqual(main.filter_outliers(prices), prices)

    def test_condition_groups(self):
        self.assertEqual(main.condition_group("Brand New"), "new")
        self.assertEqual(main.condition_group("Used - Good"), "used")
        self.assertEqual(main.condition_group("Gebraucht - Gut"), "used")
        self.assertEqual(main.condition_group("Neu"), "new")
        self.assertEqual(main.condition_group("For parts or not working"), "unknown")


if __name__ == "__main__":
    unittest.main()
