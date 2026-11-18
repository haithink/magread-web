"""Shared browser interactions that follow the responsive reader toolbar."""


def reader_button(page,name):
    target=page.locator('.reader-toolbar').get_by_role('button',name=name,exact=True)
    if not target.is_visible():
        page.locator('#reader-more-toggle').click()
    return target
