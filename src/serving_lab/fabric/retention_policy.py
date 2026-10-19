"""Compact cache inventory for the current attention table."""


def retain_images(images, table, retained):
    visible = {(r['page_id'],r['version']) for r in table}
    hints = {(r['page_id'],r['version']) for r in retained}
    selected = visible | (hints & visible)
    return {key:value for key,value in images.items() if key in selected}


def inventory_cost(images):
    return len({key[0] for key in images})
