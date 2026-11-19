"""Render the existing book SVG into home-screen PNGs; no external assets/network."""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[1] / 'webapp' / 'static'
    svg = (root / 'icon.svg').read_text(encoding='utf-8')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, **(
            {'executable_path': os.environ['KANDU_TEST_CHROME']} if os.environ.get('KANDU_TEST_CHROME') else {}))
        page = browser.new_page()
        for name, size, maskable in [('icon-180.png', 180, False), ('icon-192.png', 192, False),
                                     ('icon-512.png', 512, False), ('icon-maskable-512.png', 512, True)]:
            page.set_viewport_size(dict(width=size, height=size))
            # Keep the book within the central 80% safe region for maskable icons.
            width = '80%' if maskable else '100%'
            page.set_content('<style>html,body{margin:0;width:100%;height:100%;background:#183f35}'
                             'body{display:grid;place-items:center}svg{width:' + width + ';height:' + width + '}</style>' + svg)
            page.screenshot(path=str(root / name))
            print(name)
        browser.close()


if __name__ == '__main__':
    main()
