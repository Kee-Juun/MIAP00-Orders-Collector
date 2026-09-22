import unittest

from selenium.webdriver.chrome.options import Options

from browser.webdriver_factory import _configure_options, browser_mode_label


class WebDriverFactoryTests(unittest.TestCase):
    def test_background_mode_adds_headless_argument_and_label(self):
        options = Options()
        _configure_options(options, headless=True, download_dir=None)

        self.assertIn("--headless=new", options.arguments)
        self.assertEqual(browser_mode_label(True), "Background (headless)")

    def test_visible_mode_omits_headless_argument_and_label(self):
        options = Options()
        _configure_options(options, headless=False, download_dir=None)

        self.assertNotIn("--headless=new", options.arguments)
        self.assertEqual(browser_mode_label(False), "Visible windows")


if __name__ == "__main__":
    unittest.main()
